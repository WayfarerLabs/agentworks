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
@pytest.mark.parametrize("effect", [False, True])
@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
def test_publication_interrupt_retains_effect_facts_after_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: Create | Replace,
    effect: bool,
    failure: type[BaseException],
) -> None:
    destination = tmp_path / "download"
    if isinstance(condition, Replace):
        destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=condition)
    writer.try_write(memoryview(b"new"))
    operation = "link" if isinstance(condition, Create) else "replace"
    publish = getattr(os, operation)

    def interrupt(*args: object, **kwargs: object) -> None:
        assert writer.publication_uncertain
        if effect:
            publish(*args, **kwargs)
        raise failure

    monkeypatch.setattr(os, operation, interrupt)
    with pytest.raises(failure):
        commit(writer, b"new")
    writer.abort()
    assert writer.published is effect
    assert writer.publication_uncertain is not effect
    assert not list(tmp_path.glob(".agw-download-*"))
    if effect or isinstance(condition, Replace):
        assert destination.read_bytes() == (b"new" if effect else b"old")
    else:
        assert not destination.exists()


@pytest.mark.parametrize("condition", [Create(), Replace()])
def test_publication_inspection_failure_keeps_uncertainty_after_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: Create | Replace
) -> None:
    destination = tmp_path / "download"
    if isinstance(condition, Replace):
        destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication(destination, condition=condition)
    writer.try_write(memoryview(b"new"))
    operation = "link" if isinstance(condition, Create) else "replace"
    publish = getattr(os, operation)
    inspect = os.stat

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError

    def interrupt(*args: object, **kwargs: object) -> None:
        publish(*args, **kwargs)
        monkeypatch.setattr(os, "stat", refuse)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, operation, interrupt)
    with pytest.raises(KeyboardInterrupt):
        commit(writer, b"new")
    assert not writer.published and writer.publication_uncertain
    with pytest.raises(ValueError):
        writer.try_write(memoryview(b"more"))
    with pytest.raises(ValueError):
        commit(writer, b"new")
    monkeypatch.setattr(os, "stat", inspect)
    writer.abort()
    assert not writer.published and writer.publication_uncertain
    assert destination.read_bytes() == b"new"
    assert not list(tmp_path.glob(".agw-download-*"))


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
    assert not writer.publication_uncertain
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


def test_abort_refuses_changed_stage_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = local.LocalDownloadPublication(tmp_path / "download")
    writer.try_write(memoryview(b"new"))

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(os, "link", refuse)
    with pytest.raises(OSError):
        commit(writer, b"new")
    stage = next(tmp_path.iterdir())
    owned = tmp_path / "owned"
    stage.rename(owned)
    stage.write_bytes(b"unrelated")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        writer.abort()
    assert not writer.published and writer.publication_uncertain
    assert stage.read_bytes() == b"unrelated"
    assert owned.exists()
    os.replace(owned, stage)
    writer.abort()
    assert not writer.published and writer.publication_uncertain
    assert not list(tmp_path.iterdir())


class _InterruptBeforeCloseAdmission(local.LocalDownloadPublication):
    interrupt_name: str | None = None

    def __setattr__(self, name: str, value: object) -> None:
        if name == self.interrupt_name and value is True:
            super().__setattr__("interrupt_name", None)
            raise KeyboardInterrupt
        super().__setattr__(name, value)


@pytest.mark.parametrize("handle", ["stage", "parent"])
@pytest.mark.parametrize("publish", [False, True])
def test_interruption_before_close_admission_retains_retry_custody(tmp_path: Path, handle: str, publish: bool) -> None:
    writer = _InterruptBeforeCloseAdmission(tmp_path / "download")
    writer.try_write(memoryview(b"new"))
    fd = writer._stage_fd if handle == "stage" else writer._parent_fd
    assert fd is not None
    identity = os.fstat(fd)
    writer.interrupt_name = f"_{handle}_close_uncertain"
    with pytest.raises(KeyboardInterrupt):
        if publish:
            commit(writer, b"new")
        else:
            writer.abort()
    assert not writer.cleanup_uncertain
    assert os.fstat(fd).st_ino == identity.st_ino
    writer.abort()
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not writer.cleanup_uncertain
    assert writer.published is publish
    assert not writer.publication_uncertain
    assert not list(tmp_path.glob(".agw-download-*"))


@pytest.mark.parametrize("during_construction", [False, True])
def test_metadata_close_interruption_before_admission_cleans_with_retry(
    tmp_path: Path, during_construction: bool
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = _InterruptBeforeCloseAdmission.__new__(_InterruptBeforeCloseAdmission)
    if not during_construction:
        _InterruptBeforeCloseAdmission.__init__(writer, destination, condition=Replace())
        writer.try_write(memoryview(b"new"))
    writer.interrupt_name = "_metadata_close_uncertain"
    with pytest.raises(KeyboardInterrupt):
        if during_construction:
            _InterruptBeforeCloseAdmission.__init__(writer, destination, condition=Replace())
        else:
            commit(writer, b"new")
    assert not writer.cleanup_uncertain
    writer.abort()
    assert writer._metadata_fd is None and writer._stage_fd is None and writer._parent_fd is None
    assert not writer.cleanup_uncertain
    assert not writer.published and not writer.publication_uncertain
    assert list(tmp_path.iterdir()) == [destination]
    assert destination.read_bytes() == b"old"


@pytest.mark.parametrize("during_construction", [False, True])
@pytest.mark.parametrize("effect", [False, True])
def test_ambiguous_metadata_close_is_visible_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_construction: bool, effect: bool
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = local.LocalDownloadPublication.__new__(local.LocalDownloadPublication)
    if not during_construction:
        local.LocalDownloadPublication.__init__(writer, destination, condition=Replace())
        writer.try_write(memoryview(b"new"))
    close = os.close
    interrupted: list[int] = []
    unrelated = tmp_path / "unrelated"

    def interrupt(fd: int) -> None:
        if fd != writer._metadata_fd:
            close(fd)
            return
        interrupted.append(fd)
        if effect:
            close(fd)
            replacement = os.open(unrelated, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            if replacement != fd:
                os.dup2(replacement, fd)
                close(replacement)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "close", interrupt)
    try:
        if during_construction:
            with pytest.raises(local.LocalDownloadCleanupUncertainError) as error:
                local.LocalDownloadPublication.__init__(writer, destination, condition=Replace())
            assert error.value.cleanup_uncertain
        else:
            with pytest.raises(KeyboardInterrupt):
                commit(writer, b"new")
        assert writer.cleanup_uncertain
        with pytest.raises(local.LocalDownloadCleanupUncertainError) as error:
            writer.abort()
        assert error.value.cleanup_uncertain and writer.cleanup_uncertain
        assert len(interrupted) == 1
        assert not writer.published and not writer.publication_uncertain
        assert writer._stage_fd is None and writer._parent_fd is None
        remaining = os.fstat(interrupted[0])
        expected = unrelated.stat() if effect else destination.stat()
        assert (remaining.st_dev, remaining.st_ino) == (expected.st_dev, expected.st_ino)
        assert destination.read_bytes() == b"old"
        assert not list(tmp_path.glob(".agw-download-*"))
    finally:
        if interrupted:
            close(interrupted[0])


@pytest.mark.parametrize("handle", ["stage", "parent"])
@pytest.mark.parametrize("effect", [False, True])
@pytest.mark.parametrize("publish", [False, True])
def test_ambiguous_close_never_retries_a_possibly_reused_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handle: str, effect: bool, publish: bool
) -> None:
    writer = local.LocalDownloadPublication(tmp_path / "download")
    writer.try_write(memoryview(b"new"))
    fd = writer._stage_fd if handle == "stage" else writer._parent_fd
    assert fd is not None
    identity = os.fstat(fd)
    close = os.close
    attempts: list[int] = []
    unrelated = tmp_path / "unrelated"

    def interrupt(closing: int) -> None:
        attempts.append(closing)
        if closing != fd:
            close(closing)
            return
        assert writer.cleanup_uncertain
        if effect:
            close(closing)
            replacement = os.open(unrelated, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            if replacement != fd:
                os.dup2(replacement, fd)
                close(replacement)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "close", interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            if publish:
                commit(writer, b"new")
            else:
                writer.abort()
        with pytest.raises(local.LocalDownloadUnsupportedError):
            writer.abort()
        assert attempts.count(fd) == 1
        assert writer.cleanup_uncertain
        assert writer.published is publish
        assert not writer.publication_uncertain
        remaining = os.fstat(fd)
        assert (remaining.st_dev, remaining.st_ino) == (
            (unrelated.stat().st_dev, unrelated.stat().st_ino) if effect else (identity.st_dev, identity.st_ino)
        )
        with pytest.raises(ValueError):
            writer.try_write(memoryview(b"more"))
        assert not list(tmp_path.glob(".agw-download-*"))
    finally:
        # The injected effect is known to this fixture; the writer cannot know
        # whether its descriptor closed and must not attempt this recovery.
        close(fd)


def test_unsupported_host_refuses_before_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(local.LocalDownloadUnsupportedError):
        local.LocalDownloadPublication(tmp_path / "download")
    assert not list(tmp_path.iterdir())
