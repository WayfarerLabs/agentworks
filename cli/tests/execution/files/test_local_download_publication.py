"""Native Linux local staging, publication and metadata refusal boundaries."""

from __future__ import annotations

import array
import hashlib
import os
import secrets
import stat
import struct
import sys
from pathlib import Path

import pytest

from agentworks.execution import _local_download_publication as local
from agentworks.execution.files import Create, Replace

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "linux", reason="Linux publication evidence")]


def commit(writer: local.LocalDownloadPublication, data: bytes) -> None:
    writer.commit(verified_complete=True, size=len(data), sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("data", [b"", b"downloaded bytes\x00\xff"])
def test_create_stages_beside_destination_and_publishes_only_when_verified(tmp_path: Path, data: bytes) -> None:
    destination = tmp_path / "download"
    writer = local.LocalDownloadPublication(destination)
    try:
        stages = list(tmp_path.iterdir())
        assert len(stages) == 1
        assert stat.S_IMODE(stages[0].stat().st_mode) == 0o600
        assert stages[0].stat().st_uid == os.geteuid()
        assert not destination.exists()
        assert writer.try_write(memoryview(data)) == len(data)
        assert not destination.exists()
        commit(writer, data)
        assert writer.published
        assert destination.read_bytes() == data
        assert list(tmp_path.iterdir()) == [destination]
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        with pytest.raises(ValueError):
            writer.try_write(memoryview(b"more"))
    finally:
        writer.abort()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "hardlink", "fifo"])
@pytest.mark.parametrize("condition", [Create(), Replace()])
def test_refuses_incompatible_existing_destinations(tmp_path: Path, kind: str, condition: Create | Replace) -> None:
    destination = tmp_path / "download"
    other = tmp_path / "other"
    if kind == "directory":
        destination.mkdir()
    elif kind == "symlink":
        destination.symlink_to(other)
    elif kind == "hardlink":
        other.write_bytes(b"old")
        os.link(other, destination)
    elif kind == "fifo":
        os.mkfifo(destination)
    else:
        destination.write_bytes(b"old")
    if isinstance(condition, Replace) and kind == "file":
        writer = local.LocalDownloadPublication(destination, condition=condition)
        writer.abort()
    else:
        with pytest.raises(OSError):
            local.LocalDownloadPublication(destination, condition=condition)
    assert not list(tmp_path.glob(".agw-download-*"))
    assert os.path.lexists(destination)


def test_replace_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        local.LocalDownloadPublication(tmp_path / "absent", condition=Replace())
    assert not list(tmp_path.iterdir())


def test_replace_preserves_workstation_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    destination.chmod(0o640)
    os.setxattr(destination, "user.local-download", b"private local metadata")
    os.utime(destination, ns=(1_000_000_000, 2_000_000_000))
    before = destination.stat()
    writer = local.LocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(b"new"))
        commit(writer, b"new")
        after = destination.stat()
        assert destination.read_bytes() == b"new"
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid,
            before.st_gid,
            stat.S_IMODE(before.st_mode),
        )
        assert after.st_ino != before.st_ino
        assert after.st_mtime_ns > before.st_mtime_ns
        assert os.getxattr(destination, "user.local-download") == b"private local metadata"
    finally:
        writer.abort()


def test_replace_preserves_native_posix_acl(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    # Linux POSIX ACL xattr version 2: owner, named user, group, mask, other.
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identity)
        for tag, permissions, identity in (
            (1, 6, 0xFFFFFFFF),
            (2, 4, os.geteuid()),
            (4, 4, 0xFFFFFFFF),
            (16, 4, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        )
    )
    os.setxattr(destination, "system.posix_acl_access", acl)
    before = os.getxattr(destination, "system.posix_acl_access")
    writer = local.LocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(b"new"))
        commit(writer, b"new")
        assert destination.read_bytes() == b"new"
        assert os.getxattr(destination, "system.posix_acl_access") == before
        assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    finally:
        writer.abort()


@pytest.mark.parametrize("mode", [0o4750, 0o2750, 0o6750])
def test_replace_refuses_set_id_files(tmp_path: Path, mode: int) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    destination.chmod(mode)
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(destination, condition=Replace())
    assert destination.read_bytes() == b"old"
    assert stat.S_IMODE(destination.stat().st_mode) == mode
    assert not list(tmp_path.glob(".agw-download-*"))


@pytest.mark.parametrize("change_during_transfer", [False, True])
def test_replace_requires_native_destination_write_access(tmp_path: Path, change_during_transfer: bool) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=Replace()) if change_during_transfer else None
    destination.chmod(0o400)
    try:
        # The actual host permission check is the behavior Replace must honor.
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except PermissionError:
            pass
        else:
            os.close(fd)
            pytest.skip("Native process privileges bypass file write permissions")
        with pytest.raises(PermissionError):
            if writer is None:
                local.LocalDownloadPublication(destination, condition=Replace())
            else:
                writer.try_write(memoryview(b"new"))
                commit(writer, b"new")
        assert destination.read_bytes() == b"old"
    finally:
        if writer is not None:
            writer.abort()
    assert not list(tmp_path.glob(".agw-download-*"))


@pytest.mark.parametrize("change_during_transfer", [False, True])
def test_replace_refuses_native_nodump_inode_flag(tmp_path: Path, change_during_transfer: bool) -> None:
    import fcntl

    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=Replace()) if change_during_transfer else None
    fd = os.open(destination, os.O_WRONLY)
    flags = array.array("L", [0])
    get_flags = 0x80000000 | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
    set_flags = 0x40000000 | (struct.calcsize("l") << 16) | (ord("f") << 8) | 2
    try:
        fcntl.ioctl(fd, get_flags, flags, True)
        original = flags[0]
        flags[0] |= 0x40  # FS_NODUMP_FL, the attribute selected by chattr +d.
        fcntl.ioctl(fd, set_flags, flags, True)
        with pytest.raises(local.LocalDownloadUnsupportedError):
            if writer is None:
                local.LocalDownloadPublication(destination, condition=Replace())
            else:
                writer.try_write(memoryview(b"new"))
                commit(writer, b"new")
        assert destination.read_bytes() == b"old"
        fcntl.ioctl(fd, get_flags, flags, True)
        assert flags[0] == original | 0x40
    finally:
        if writer is not None:
            writer.abort()
        flags[0] = original
        fcntl.ioctl(fd, set_flags, flags, True)
        os.close(fd)
    assert not list(tmp_path.glob(".agw-download-*"))


def test_inode_flags_inspection_failure_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    destination = tmp_path / "download"
    destination.write_bytes(b"old")

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(fcntl, "ioctl", refuse)
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(destination, condition=Replace())
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("name", ["security.capability", "security.selinux", "security.ima"])
def test_security_xattrs_refuse_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    monkeypatch.setattr(os, "listxattr", lambda fd: [name])
    monkeypatch.setattr(os, "getxattr", lambda fd, name: b"security metadata")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(destination, condition=Replace())
    assert list(tmp_path.iterdir()) == [destination]


def test_native_file_capability_refuses_before_staging(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    capability = struct.pack("<5I", 0x02000001, 1 << 10, 0, 0, 0)
    try:
        os.setxattr(destination, "security.capability", capability)
    except PermissionError:
        pytest.skip("Native capability fixture requires CAP_SETFCAP")
    before = os.getxattr(destination, "security.capability")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(destination, condition=Replace())
    assert destination.read_bytes() == b"old"
    assert os.getxattr(destination, "security.capability") == before
    assert not list(tmp_path.glob(".agw-download-*"))


def test_parent_rename_is_refused_and_abort_uses_bound_directory(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    writer = local.LocalDownloadPublication(parent / "download")
    writer.try_write(memoryview(b"new"))
    moved = tmp_path / "moved"
    parent.rename(moved)
    parent.mkdir()
    try:
        with pytest.raises(FileExistsError):
            commit(writer, b"new")
    finally:
        writer.abort()
    assert not list(parent.iterdir())
    assert not list(moved.iterdir())


@pytest.mark.parametrize("verified,size,digest", [(False, 3, "correct"), (True, 2, "correct"), (True, 3, "wrong")])
def test_incomplete_verification_cannot_publish(tmp_path: Path, verified: bool, size: int, digest: str) -> None:
    destination = tmp_path / "download"
    writer = local.LocalDownloadPublication(destination)
    writer.try_write(memoryview(b"new"))
    try:
        with pytest.raises(ValueError):
            writer.commit(
                verified_complete=verified,
                size=size,
                sha256=hashlib.sha256(b"new").hexdigest() if digest == "correct" else digest,
            )
        assert not destination.exists()
        assert not writer.published
    finally:
        writer.abort()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("condition", [Create(), Replace()])
def test_destination_race_is_rejected_when_observed(tmp_path: Path, condition: Create | Replace) -> None:
    destination = tmp_path / "download"
    if isinstance(condition, Replace):
        destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=condition)
    writer.try_write(memoryview(b"new"))
    raced = tmp_path / "raced"
    raced.write_bytes(b"external")
    os.replace(raced, destination)
    try:
        with pytest.raises(FileExistsError):
            commit(writer, b"new")
        assert destination.read_bytes() == b"external"
        assert not writer.published
    finally:
        writer.abort()
    assert list(tmp_path.iterdir()) == [destination]


def test_metadata_failure_refuses_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    os.setxattr(destination, "user.local-download", b"metadata")
    writer = local.LocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new"))

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "setxattr", refuse)
    try:
        with pytest.raises(local.LocalDownloadUnsupportedError):
            commit(writer, b"new")
        assert destination.read_bytes() == b"old"
        assert os.getxattr(destination, "user.local-download") == b"metadata"
    finally:
        writer.abort()
    assert list(tmp_path.iterdir()) == [destination]


def test_write_failure_can_abort_without_removing_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=Replace())

    def refuse(*args: object, **kwargs: object) -> int:
        raise OSError

    monkeypatch.setattr(os, "write", refuse)
    try:
        with pytest.raises(OSError):
            writer.try_write(memoryview(b"new"))
    finally:
        writer.abort()
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [destination]


def test_short_writes_track_only_accepted_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    writer = local.LocalDownloadPublication(destination)
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:2]))
    try:
        assert writer.try_write(memoryview(b"abcd")) == 2
        assert writer.try_write(memoryview(b"cd")) == 2
        commit(writer, b"abcd")
        assert destination.read_bytes() == b"abcd"
    finally:
        writer.abort()


def test_cleanup_failure_retains_published_fact_and_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    writer = local.LocalDownloadPublication(destination)
    writer.try_write(memoryview(b"new"))
    unlink = os.unlink

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "unlink", refuse)
    with pytest.raises(PermissionError):
        commit(writer, b"new")
    assert writer.published
    assert destination.read_bytes() == b"new"
    monkeypatch.setattr(os, "unlink", unlink)
    writer.abort()
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("condition", [Create(), Replace()])
def test_sync_failure_leaves_destination_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: Create | Replace
) -> None:
    destination = tmp_path / "download"
    if isinstance(condition, Replace):
        destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=condition)
    writer.try_write(memoryview(b"new"))

    def refuse(fd: int) -> None:
        raise OSError

    monkeypatch.setattr(os, "fsync", refuse)
    try:
        with pytest.raises(OSError):
            commit(writer, b"new")
        assert not writer.published
    finally:
        writer.abort()
    if isinstance(condition, Replace):
        assert destination.read_bytes() == b"old"
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".agw-download-*"))


def test_unreadable_metadata_refuses_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")

    def refuse(fd: int) -> list[str]:
        raise PermissionError

    monkeypatch.setattr(os, "listxattr", refuse)
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(destination, condition=Replace())
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [destination]


def test_stage_name_collision_preserves_other_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collision = tmp_path / ".agw-download-collision"
    collision.write_bytes(b"unrelated")
    names = iter(("collision", "available"))
    monkeypatch.setattr(secrets, "token_hex", lambda size: next(names))
    writer = local.LocalDownloadPublication(tmp_path / "download")
    writer.abort()
    assert collision.read_bytes() == b"unrelated"
    assert list(tmp_path.iterdir()) == [collision]


def test_abort_refuses_changed_stage_identity(tmp_path: Path) -> None:
    writer = local.LocalDownloadPublication(tmp_path / "download")
    stage = next(tmp_path.iterdir())
    owned = tmp_path / "owned"
    stage.rename(owned)
    stage.write_bytes(b"unrelated")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        writer.abort()
    assert stage.read_bytes() == b"unrelated"
    assert owned.exists()
    os.replace(owned, stage)
    writer.abort()
    assert not list(tmp_path.iterdir())


def test_unsupported_host_refuses_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(tmp_path / "download")
    assert not list(tmp_path.iterdir())
