"""Native Linux local staging, publication and metadata refusal boundaries."""

from __future__ import annotations

import hashlib
import os
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
        assert (after.st_atime_ns, after.st_mtime_ns) == (before.st_atime_ns, before.st_mtime_ns)
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

    monkeypatch.setattr(local.os, "setxattr", refuse)
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

    monkeypatch.setattr(local.os, "write", refuse)
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
    monkeypatch.setattr(local.os, "write", lambda fd, data: write(fd, data[:2]))
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

    monkeypatch.setattr(local.os, "unlink", refuse)
    with pytest.raises(PermissionError):
        commit(writer, b"new")
    assert writer.published
    assert destination.read_bytes() == b"new"
    monkeypatch.setattr(local.os, "unlink", unlink)
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

    monkeypatch.setattr(local.os, "fsync", refuse)
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

    monkeypatch.setattr(local.os, "listxattr", refuse)
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(destination, condition=Replace())
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [destination]


def test_stage_name_collision_preserves_other_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    collision = tmp_path / ".agw-download-collision"
    collision.write_bytes(b"unrelated")
    names = iter(("collision", "available"))
    monkeypatch.setattr(local.secrets, "token_hex", lambda size: next(names))
    writer = local.LocalDownloadPublication(tmp_path / "download")
    writer.abort()
    assert collision.read_bytes() == b"unrelated"
    assert list(tmp_path.iterdir()) == [collision]


def test_unsupported_host_refuses_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local.sys, "platform", "win32")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(tmp_path / "download")
    assert not list(tmp_path.iterdir())
