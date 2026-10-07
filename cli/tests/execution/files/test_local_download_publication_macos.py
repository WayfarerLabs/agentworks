"""Portable fault checks for the private macOS publisher; native proof is separate."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.execution import _local_download_publication_macos as local
from agentworks.execution._local_download_publication import (
    LocalDownloadCleanupUncertainError,
    LocalDownloadUnsupportedError,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Create, Replace

_REAL_HAS_EXTENDED_ACL = local._has_extended_acl
_REAL_HAS_NON_DENY_ACL = local._has_non_deny_acl


@pytest.fixture(autouse=True)
def emulate_darwin_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(local, "_bsd_flags", lambda observed: 0)
        monkeypatch.setattr(local, "_has_extended_acl", lambda fd: False)
        monkeypatch.setattr(local, "_has_non_deny_acl", lambda fd: False)
        monkeypatch.setattr(local, "_has_xattrs", lambda fd: False)


def _commit(writer: local.MacOSLocalDownloadPublication, data: bytes) -> None:
    writer.commit(verified_complete=True, size=len(data), sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("data", [b"", b"new bytes\x00\xff"])
def test_create_is_private_and_no_replace(tmp_path: Path, data: bytes) -> None:
    destination = tmp_path / "download"
    writer = local.MacOSLocalDownloadPublication(destination)
    try:
        assert writer.try_write(memoryview(data)) == len(data)
        assert not destination.exists()
        stage = next(tmp_path.glob(".agw-download-*"))
        assert stat.S_IMODE(stage.stat().st_mode) == 0o600
        _commit(writer, data)
        assert writer.published
        assert not writer.publication_uncertain
        assert destination.read_bytes() == data
        assert list(tmp_path.iterdir()) == [destination]
    finally:
        writer.abort()


def test_create_refuses_existing_path_even_if_it_appears_during_transfer(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    writer = local.MacOSLocalDownloadPublication(destination)
    try:
        writer.try_write(memoryview(b"new"))
        destination.write_bytes(b"old")
        with pytest.raises(FileExistsError):
            _commit(writer, b"new")
        assert destination.read_bytes() == b"old"
        assert not writer.published
        assert writer.publication_uncertain
    finally:
        writer.abort()
    assert list(tmp_path.iterdir()) == [destination]


def test_replace_keeps_held_inode_and_plain_access_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"original longer data")
    destination.chmod(0o640)
    before = destination.stat()
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(b"new"))
        assert destination.read_bytes() == b"original longer data"
        _commit(writer, b"new")
        after = destination.stat()
        assert writer.published
        assert not writer.publication_uncertain
        assert destination.read_bytes() == b"new"
        assert (after.st_dev, after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
        )
    finally:
        writer.abort()


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "hardlink", "fifo"])
def test_replace_refuses_nonordinary_targets_before_stage(tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "download"
    other = tmp_path / "other"
    if kind == "symlink":
        destination.symlink_to(other)
    elif kind == "directory":
        destination.mkdir()
    elif kind == "hardlink":
        other.write_bytes(b"old")
        os.link(other, destination)
    elif kind == "fifo":
        os.mkfifo(destination)
    with pytest.raises(OSError):
        local.MacOSLocalDownloadPublication(destination, condition=Replace())
    assert not list(tmp_path.glob(".agw-download-*"))


def test_replace_refuses_xattrs_before_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    if not hasattr(os, "setxattr"):
        pytest.skip("Native extended attribute fixture requires a Darwin setter")
    try:
        os.setxattr(destination, "user.local-download", b"metadata")
    except OSError:
        pytest.skip("Test filesystem cannot set an extended attribute")
    monkeypatch.setattr(local, "_has_xattrs", lambda fd: bool(os.listxattr(fd)))
    with pytest.raises(LocalDownloadUnsupportedError):
        local.MacOSLocalDownloadPublication(destination, condition=Replace())
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".agw-download-*"))


def test_replace_refuses_unverified_transfer_without_touching_target(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(b"new"))
        with pytest.raises(ValueError):
            writer.commit(verified_complete=False, size=3, sha256=hashlib.sha256(b"new").hexdigest())
        assert destination.read_bytes() == b"old"
        assert not writer.publication_uncertain
    finally:
        writer.abort()


def test_expired_deadline_refuses_before_local_mutation(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(b"new"))
        with pytest.raises(TimeoutError):
            writer.commit(
                verified_complete=True,
                size=3,
                sha256=hashlib.sha256(b"new").hexdigest(),
                deadline=Deadline.after(0),
            )
        assert destination.read_bytes() == b"old"
    finally:
        writer.abort()


@pytest.mark.parametrize("unsupported", ["acl", "flags", "setid"])
def test_replace_refuses_unsupported_access_state_before_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: str
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    if unsupported == "acl":
        monkeypatch.setattr(local, "_has_extended_acl", lambda fd: stat.S_ISREG(os.fstat(fd).st_mode))
    elif unsupported == "flags":
        monkeypatch.setattr(local, "_bsd_flags", lambda observed: int(stat.S_ISREG(observed.st_mode)))
    else:
        destination.chmod(0o4750)
    with pytest.raises(LocalDownloadUnsupportedError):
        local.MacOSLocalDownloadPublication(destination, condition=Replace())
    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob(".agw-download-*"))


def test_partial_write_retains_possible_local_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old content")
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new data"))
    target_fd = writer._target_fd
    original_write = os.write
    calls = 0

    def partial_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        if fd == target_fd:
            calls += 1
            if calls == 1:
                return original_write(fd, data[:2])
            raise OSError("injected local write failure")
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", partial_write)
    try:
        with pytest.raises(OSError):
            _commit(writer, b"new data")
        assert destination.read_bytes().startswith(b"ne")
        assert not writer.published
        assert writer.publication_uncertain
    finally:
        writer.abort()
    assert not list(tmp_path.glob(".agw-download-*"))


def test_deadline_after_first_write_retains_possible_local_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old content")
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new"))
    deadline = SimpleNamespace(expired=False)
    target_fd = writer._target_fd
    original_write = os.write

    def expire_after_write(fd: int, data: bytes | memoryview) -> int:
        result = original_write(fd, data)
        if fd == target_fd:
            deadline.expired = True
        return result

    monkeypatch.setattr(os, "write", expire_after_write)
    try:
        with pytest.raises(TimeoutError):
            writer.commit(
                verified_complete=True,
                size=3,
                sha256=hashlib.sha256(b"new").hexdigest(),
                deadline=deadline,
            )
        assert not writer.published
        assert writer.publication_uncertain
        assert destination.read_bytes().startswith(b"new")
    finally:
        writer.abort()


def test_close_failure_after_replace_retains_uncertainty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new"))
    target_fd = writer._target_fd
    original_close = os.close

    def failed_close(fd: int) -> None:
        if fd == target_fd:
            raise OSError("injected ambiguous close")
        original_close(fd)

    monkeypatch.setattr(os, "close", failed_close)
    with pytest.raises(OSError):
        _commit(writer, b"new")
    assert writer.publication_uncertain
    assert writer.cleanup_uncertain
    with pytest.raises(LocalDownloadCleanupUncertainError):
        writer.abort()
    assert not list(tmp_path.glob(".agw-download-*"))
    monkeypatch.setattr(os, "close", original_close)
    if target_fd is not None:
        original_close(target_fd)


def test_deadline_after_target_close_retains_possible_local_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.MacOSLocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new"))
    deadline = SimpleNamespace(expired=False)
    target_fd = writer._target_fd
    original_close = os.close

    def expire_after_close(fd: int) -> None:
        original_close(fd)
        if fd == target_fd:
            deadline.expired = True

    monkeypatch.setattr(os, "close", expire_after_close)
    try:
        with pytest.raises(TimeoutError):
            writer.commit(
                verified_complete=True,
                size=3,
                sha256=hashlib.sha256(b"new").hexdigest(),
                deadline=deadline,
            )
        assert writer.publication_uncertain
        assert not writer.published
        assert not writer.cleanup_uncertain
        assert destination.read_bytes() == b"new"
    finally:
        writer.abort()
    assert not list(tmp_path.glob(".agw-download-*"))


def test_rejects_symlink_ancestor_and_world_writable_parent(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(OSError):
        local.MacOSLocalDownloadPublication(alias / "download", condition=Create())
    actual.chmod(0o777)
    try:
        with pytest.raises(LocalDownloadUnsupportedError):
            local.MacOSLocalDownloadPublication(actual / "download")
    finally:
        actual.chmod(0o700)
    assert not list(actual.iterdir())


def test_rejects_non_normal_destination_before_staging(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        local.MacOSLocalDownloadPublication(tmp_path / "other" / ".." / "download")
    assert not list(tmp_path.iterdir())


def test_rejects_acl_ancestor_before_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "_has_non_deny_acl", lambda fd: os.fstat(fd).st_ino == tmp_path.stat().st_ino)
    with pytest.raises(LocalDownloadUnsupportedError):
        local.MacOSLocalDownloadPublication(tmp_path / "download")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("tags", "unsupported"),
    [([], False), ([2], False), ([2, 2], False), ([1], True), ([2, 1], True), ([3], True), ([0], True)],
)
def test_directory_acl_admits_only_deny_entries(
    monkeypatch: pytest.MonkeyPatch, tags: list[int], unsupported: bool
) -> None:
    monkeypatch.setattr(local, "_has_extended_acl", _REAL_HAS_EXTENDED_ACL)
    monkeypatch.setattr(local, "_has_non_deny_acl", _REAL_HAS_NON_DENY_ACL)
    freed: list[int] = []

    def get_acl(fd: int, acl_type: int) -> int:
        assert (fd, acl_type) == (42, local._ACL_TYPE_EXTENDED)
        return 77

    def free_acl(acl: int) -> int:
        freed.append(acl)
        return 0

    def get_entry(acl: int, index: int, entry_p: object) -> int:
        assert acl == 77
        if index == len(tags):
            ctypes.set_errno(errno.EINVAL)
            return -1
        ctypes.cast(entry_p, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(index + 1)
        return 0

    def get_tag(entry: ctypes.c_void_p, tag_p: object) -> int:
        assert entry.value is not None
        ctypes.cast(tag_p, ctypes.POINTER(ctypes.c_int))[0] = tags[entry.value - 1]
        return 0

    def valid_acl(acl: int) -> int:
        assert acl == 77
        return 0

    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            acl_get_fd_np=get_acl,
            acl_free=free_acl,
            acl_get_entry=get_entry,
            acl_get_tag_type=get_tag,
            acl_valid=valid_acl,
        ),
    )
    assert local._has_non_deny_acl(42) is unsupported
    assert local._has_extended_acl(42) is bool(tags)
    assert freed == [77, 77]


@pytest.mark.parametrize("failure", ["get", "invalid", "entry", "null_entry", "tag", "free", "too_many"])
def test_directory_acl_inspection_refuses_uncertain_or_malformed_results(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(local, "_has_non_deny_acl", _REAL_HAS_NON_DENY_ACL)
    freed: list[int] = []

    def get_acl(fd: int, acl_type: int) -> int:
        del fd, acl_type
        if failure == "get":
            ctypes.set_errno(errno.EIO)
            return 0
        return 77

    def free_acl(acl: int) -> int:
        freed.append(acl)
        return -1 if failure == "free" else 0

    def get_entry(acl: int, index: int, entry_p: object) -> int:
        del acl
        if failure == "entry":
            ctypes.set_errno(errno.EIO)
            return -1
        if failure != "too_many" and index == 1:
            ctypes.set_errno(errno.EINVAL)
            return -1
        if failure != "null_entry":
            ctypes.cast(entry_p, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(index + 1)
        return 0

    def get_tag(entry: int, tag_p: object) -> int:
        del entry
        if failure == "tag":
            return -1
        ctypes.cast(tag_p, ctypes.POINTER(ctypes.c_int))[0] = 2
        return 0

    def valid_acl(acl: int) -> int:
        del acl
        return -1 if failure == "invalid" else 0

    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            acl_get_fd_np=get_acl,
            acl_free=free_acl,
            acl_get_entry=get_entry,
            acl_get_tag_type=get_tag,
            acl_valid=valid_acl,
        ),
    )
    with pytest.raises(LocalDownloadUnsupportedError):
        local._has_non_deny_acl(42)
    assert freed == ([] if failure == "get" else [77])


def test_missing_extended_acl_is_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "_has_non_deny_acl", _REAL_HAS_NON_DENY_ACL)

    def get_acl(fd: int, acl_type: int) -> int:
        del fd, acl_type
        ctypes.set_errno(errno.ENOENT)
        return 0

    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            acl_get_fd_np=get_acl,
            acl_free=lambda acl: 0,
            acl_get_entry=lambda acl, index, entry: -1,
            acl_get_tag_type=lambda entry, tag: -1,
            acl_valid=lambda acl: 0,
        ),
    )
    assert not local._has_non_deny_acl(42)


def test_inherited_deny_only_acl_allows_private_stage_but_not_replace_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "_has_extended_acl", _REAL_HAS_EXTENDED_ACL)
    monkeypatch.setattr(local, "_has_non_deny_acl", _REAL_HAS_NON_DENY_ACL)
    monkeypatch.setattr(local, "_extended_acl_tags", lambda fd: (2,))
    destination = tmp_path / "download"
    writer = local.MacOSLocalDownloadPublication(destination)
    try:
        writer.try_write(memoryview(b"new"))
        _commit(writer, b"new")
        assert destination.read_bytes() == b"new"
    finally:
        writer.abort()
    with pytest.raises(LocalDownloadUnsupportedError):
        local.MacOSLocalDownloadPublication(destination, condition=Replace())
    assert not list(tmp_path.glob(".agw-download-*"))


def test_inherited_allow_acl_refuses_private_stage_without_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local, "_has_non_deny_acl", _REAL_HAS_NON_DENY_ACL)
    monkeypatch.setattr(local, "_extended_acl_tags", lambda fd: (1,) if stat.S_ISREG(os.fstat(fd).st_mode) else (2,))
    with pytest.raises(LocalDownloadUnsupportedError):
        local.MacOSLocalDownloadPublication(tmp_path / "download")
    assert not list(tmp_path.iterdir())


def test_stage_name_collision_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collision = tmp_path / ".agw-download-collision"
    collision.write_bytes(b"unrelated")
    names = iter(("collision", "available"))
    monkeypatch.setattr(secrets, "token_hex", lambda count: next(names))
    writer = local.MacOSLocalDownloadPublication(tmp_path / "download")
    writer.abort()
    assert collision.read_bytes() == b"unrelated"
    assert list(tmp_path.iterdir()) == [collision]
