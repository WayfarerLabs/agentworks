"""Darwin branches on a real POSIX filesystem with native metadata inspection mocked.

These checks establish composition and refusal behavior, not native macOS proof.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import socket
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agentworks.execution import _darwin_descriptors as darwin
from agentworks.execution import _darwin_metadata as darwin_metadata
from agentworks.execution import _file_metadata as metadata
from agentworks.execution import _file_paths as paths
from agentworks.execution import _file_publication as publication
from agentworks.execution import _scratch_root as scratch_root
from agentworks.execution._file_inventory import inventory_directory
from agentworks.execution._file_objects import (
    FileKind,
    FileObjectError,
    FileObjectFailureKind,
    remove_file_object,
    stat_file_object,
)
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    FilePublicationError,
    Match,
    PublicationFailureKind,
    Replace,
    ScratchFileSource,
    publish_file,
)
from agentworks.execution._file_snapshot_read import read_snapshot
from agentworks.execution._publication_receipt import PublicationStageCleanupDebt, cleanup_publication_stage
from agentworks.execution._scratch import cleanup_scratch
from tests.execution.files._publication_test_support import ready_scratch


@pytest.fixture(autouse=True)
def darwin_inspection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(darwin_metadata, "bsd_flags", lambda observed: 0)
    monkeypatch.setattr(darwin, "extended_acl_tags", lambda fd: ())
    monkeypatch.setattr(darwin_metadata, "has_xattrs", lambda fd: False)


@pytest.fixture
def parent(tmp_path: Path):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _metadata() -> CreateMetadata:
    return CreateMetadata(os.geteuid(), os.getegid(), 0o640)


def _emulated_noreplace(parent_fd: int, stage_name: str, leaf_name: str) -> None:
    """Emulate the selected no-replace native effect, never check-then-rename."""
    try:
        os.link(stage_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        os.unlink(stage_name, dir_fd=parent_fd)
    except FileExistsError:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, publication.PublicationPhase.PUBLICATION) from None


@pytest.mark.parametrize("relative", ["file", "child/file"])
def test_readable_directory_walk_and_binary_snapshot(tmp_path: Path, relative: str) -> None:
    (tmp_path / "child").mkdir()
    target = tmp_path / relative
    target.write_bytes(b"binary\x00\xff")
    descriptor = paths.open_file_root(str(tmp_path))
    assert descriptor is not None
    try:
        snapshot = read_snapshot(descriptor, relative, 1024)
        assert snapshot is not None and snapshot.data == b"binary\x00\xff"
        assert not os.get_inheritable(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("relative", ["", ".", "..", "a/../file", "a//file", "/file", "file/", "nul\0"])
def test_noncanonical_descendants_refuse_before_open(parent: int, relative: str, monkeypatch) -> None:
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: pytest.fail("invalid path opened"))
    with pytest.raises(paths.ConfinedOpenError):
        paths.open_file_confined(parent, relative, os.O_RDONLY)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "socket", "hardlink"])
def test_unsupported_objects_refuse_without_following_or_waiting(tmp_path: Path, parent: int, kind: str) -> None:
    target = tmp_path / "target"
    sock: socket.socket | None = None
    if kind == "symlink":
        (tmp_path / "actual").write_bytes(b"unchanged")
        target.symlink_to(tmp_path / "actual")
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "socket":
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(target))
    else:
        (tmp_path / "actual").write_bytes(b"unchanged")
        os.link(tmp_path / "actual", target)
    try:
        with pytest.raises(FileObjectError) as raised:
            stat_file_object(parent, "target", expires_at=None)
        assert raised.value.kind is FileObjectFailureKind.UNSUPPORTED
        assert target.lstat()
    finally:
        if sock is not None:
            sock.close()


def test_search_only_directory_and_unreadable_file_refuse(tmp_path: Path, parent: int) -> None:
    child = tmp_path / "child"
    child.mkdir()
    target = tmp_path / "file"
    target.write_bytes(b"hidden")
    child.chmod(0o111)
    target.chmod(0)
    try:
        with pytest.raises(paths.ConfinedOpenError):
            paths.open_file_root(str(child))
        with pytest.raises(FileObjectError):
            stat_file_object(parent, "file", expires_at=None)
    finally:
        child.chmod(0o700)
        target.chmod(0o600)


def test_symlink_ancestor_is_never_followed(tmp_path: Path, parent: int) -> None:
    (tmp_path / "actual").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "actual", target_is_directory=True)
    with pytest.raises(paths.ConfinedOpenError):
        paths.open_file_root(str(tmp_path / "alias"))
    with pytest.raises(paths.ConfinedOpenError):
        paths.open_file_confined(parent, "alias/file", os.O_RDONLY)


def test_selected_root_can_cross_device_but_descendants_cannot(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "file").write_bytes(b"content")
    inode = root.stat().st_ino
    real_stat, real_fstat = os.stat, os.fstat

    def adjusted(observed: os.stat_result) -> os.stat_result:
        if observed.st_ino != inode:
            return observed
        return cast(
            "os.stat_result",
            SimpleNamespace(st_mode=observed.st_mode, st_dev=observed.st_dev + 1, st_ino=observed.st_ino),
        )

    monkeypatch.setattr(os, "fstat", lambda fd: adjusted(real_fstat(fd)))
    monkeypatch.setattr(os, "stat", lambda *args, **kwargs: adjusted(real_stat(*args, **kwargs)))
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os.stat})
    monkeypatch.setattr(os, "supports_follow_symlinks", os.supports_follow_symlinks | {os.stat})
    descriptor = paths.open_file_root(str(root))
    assert descriptor is not None
    try:
        with pytest.raises(paths.ConfinedOpenError) as raised:
            paths.open_file_confined(descriptor, "file", os.O_RDONLY)
        assert raised.value.kind is paths.ConfinedOpenFailure.UNSUPPORTED_OBJECT
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("tags,accepted", [((2,), True), ((2, 2), True), ((1,), False), ((2, 1), False)])
def test_ancestor_acl_policy(tmp_path: Path, tags: tuple[int, ...], accepted: bool, monkeypatch) -> None:
    monkeypatch.setattr(darwin, "extended_acl_tags", lambda fd: tags)
    if accepted:
        descriptor = paths.open_file_root(str(tmp_path))
        assert descriptor is not None
        os.close(descriptor)
    else:
        with pytest.raises(paths.ConfinedOpenError):
            paths.open_file_root(str(tmp_path))


def test_metadata_changes_held_inode_without_a_path_bridge(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"content")
    target.chmod(0o600)
    before = target.stat()
    real_stat = os.stat

    def check_path(path, *args, **kwargs):
        assert not isinstance(path, str) or not path.startswith(("/proc/", "/dev/fd/"))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", check_path)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {os.stat})
    monkeypatch.setattr(os, "supports_follow_symlinks", os.supports_follow_symlinks | {os.stat})
    result = metadata.set_metadata(parent, "file", uid=before.st_uid, gid=before.st_gid, mode=0o640, expires_at=None)
    assert result.changed
    after = target.stat()
    assert (after.st_ino, after.st_uid, after.st_gid) == (before.st_ino, before.st_uid, before.st_gid)
    assert stat.S_IMODE(after.st_mode) == 0o640 and target.read_bytes() == b"content"


def test_metadata_creation_failure_retains_completed_creation(tmp_path: Path, parent: int, monkeypatch) -> None:
    monkeypatch.setattr(os, "fchmod", lambda *args: (_ for _ in ()).throw(OSError(errno.EIO, "injected")))
    with pytest.raises(metadata.MetadataError) as raised:
        metadata.ensure_directory(parent, "directory", uid=os.geteuid(), gid=os.getegid(), mode=0o755, expires_at=None)
    assert raised.value.completed_steps == (metadata.MetadataStep.CREATION,)
    assert raised.value.attempted_step is metadata.MetadataStep.MODE
    assert raised.value.effect is metadata.MetadataEffect.UNCERTAIN
    assert (tmp_path / "directory").is_dir()


@pytest.mark.parametrize("unsupported", ["acl", "xattr", "flags", "privbits"])
def test_target_metadata_refuses_before_content_or_metadata_mutation(
    tmp_path: Path, parent: int, unsupported: str, monkeypatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    if unsupported == "acl":
        monkeypatch.setattr(darwin, "extended_acl_tags", lambda fd: (2,))
    elif unsupported == "xattr":
        monkeypatch.setattr(darwin_metadata, "has_xattrs", lambda fd: True)
    elif unsupported == "flags":
        monkeypatch.setattr(darwin_metadata, "bsd_flags", lambda observed: 1)
    else:
        target.chmod(0o4600)
    with pytest.raises(FilePublicationError):
        publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata())
    assert target.read_bytes() == b"old"
    with pytest.raises(metadata.MetadataError) as raised:
        metadata.set_metadata(parent, "target", uid=os.geteuid(), gid=os.getegid(), mode=0o640, expires_at=None)
    assert raised.value.effect is metadata.MetadataEffect.UNCHANGED


@pytest.mark.parametrize("content", [b"", b"x\x00\xff", b"longer" * 25000])
@pytest.mark.parametrize("matching", [False, True])
def test_replacement_preserves_access_and_inode(tmp_path: Path, parent: int, content: bytes, matching: bool) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old content")
    target.chmod(0o640)
    before = target.stat()
    snapshot = read_snapshot(parent, "target", 1024)
    assert snapshot is not None
    revision = publish_file(
        parent,
        "target",
        content,
        condition=Match(snapshot.revision) if matching else Replace(),
        create_metadata=CreateMetadata(os.geteuid(), os.getegid(), 0o600),
    )
    after = target.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mode) == (
        before.st_ino,
        before.st_uid,
        before.st_gid,
        before.st_mode,
    )
    assert revision.digest == hashlib.sha256(content).digest() and target.read_bytes() == content
    assert list(tmp_path.iterdir()) == [target]


def test_stale_match_refuses_without_mutation(tmp_path: Path, parent: int) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"before")
    snapshot = read_snapshot(parent, "target", 1024)
    assert snapshot is not None
    target.write_bytes(b"concurrent")
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"new", condition=Match(snapshot.revision), create_metadata=_metadata())
    assert raised.value.kind is PublicationFailureKind.CONFLICT
    assert target.read_bytes() == b"concurrent"


@pytest.mark.parametrize("fault", ["write", "truncate", "flush", "close"])
def test_replacement_failure_retains_possible_mutation(tmp_path: Path, parent: int, fault: str, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old content")
    inode = target.stat().st_ino
    real_write, real_truncate, real_flush, real_close = os.write, os.ftruncate, os.fsync, os.close
    opened: list[int] = []
    writes = 0

    def is_target(fd: int) -> bool:
        if os.fstat(fd).st_ino == inode:
            if fd not in opened:
                opened.append(fd)
            return True
        return False

    def write(fd, data):
        nonlocal writes
        if fault == "write" and is_target(fd):
            writes += 1
            if writes == 1:
                return real_write(fd, data[:2])
            raise OSError(errno.EIO, "partial write")
        return real_write(fd, data)

    def truncate(fd, length):
        if fault == "truncate" and is_target(fd):
            raise OSError(errno.EIO, "truncate")
        return real_truncate(fd, length)

    def flush(fd):
        if fault == "flush" and is_target(fd):
            raise OSError(errno.EIO, "flush")
        return real_flush(fd)

    def close(fd):
        if fault == "close" and is_target(fd):
            raise OSError(errno.EIO, "ambiguous close")
        return real_close(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", write)
        patch.setattr(os, "ftruncate", truncate)
        patch.setattr(os, "fsync", flush)
        patch.setattr(os, "close", close)
        with pytest.raises(FilePublicationError) as raised:
            publish_file(parent, "target", b"new bytes", condition=Replace(), create_metadata=_metadata())
        assert raised.value.kind is PublicationFailureKind.UNCERTAIN
    if fault == "close":
        for descriptor in opened:
            real_close(descriptor)
    assert target.read_bytes() != b"old content"
    assert list(tmp_path.iterdir()) == [target]


def test_stage_content_is_verified_before_target_write(tmp_path: Path, parent: int, monkeypatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    real_fsync = os.fsync

    def corrupt_stage(fd):
        real_fsync(fd)
        if os.fstat(fd).st_ino != target.stat().st_ino:
            os.pwrite(fd, b"bad", 0)

    monkeypatch.setattr(os, "fsync", corrupt_stage)
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"new", condition=Replace(), create_metadata=_metadata())
    assert raised.value.kind is PublicationFailureKind.CONFLICT
    assert target.read_bytes() == b"old"


def test_scratch_backed_replacement_removes_owned_stage_and_record(tmp_path: Path, parent: int) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    ready = ready_scratch(parent, bytes(range(16)), b"new\0\xff")
    revision = publish_file(
        parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
    )
    assert revision.digest == hashlib.sha256(b"new\0\xff").digest()
    cleanup_scratch(parent, ready)
    assert list(tmp_path.iterdir()) == [target]


def test_cleanup_failure_retains_exact_publication_custody(tmp_path: Path, parent: int, monkeypatch) -> None:
    (tmp_path / "target").write_bytes(b"old")
    ready = ready_scratch(parent, bytes(range(16)), b"new")
    real_unlink = os.unlink

    def fail_unlink(path, *args, **kwargs):
        if isinstance(path, str) and path.startswith(".agentworks-stage-"):
            raise OSError(errno.EIO, "stage cleanup")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_unlink)
        with pytest.raises(FilePublicationError) as raised:
            publish_file(
                parent, "target", ScratchFileSource(parent, ready), condition=Replace(), create_metadata=_metadata()
            )
    assert raised.value.kind is PublicationFailureKind.UNCERTAIN
    debt = raised.value.cleanup_debt
    assert isinstance(debt, PublicationStageCleanupDebt)
    assert (tmp_path / debt.name).stat().st_ino == debt.inode
    cleanup_publication_stage(parent, parent, debt)
    cleanup_scratch(parent, ready)
    assert [item.name for item in tmp_path.iterdir()] == ["target"]


def test_create_inventory_and_conditional_remove_compose(tmp_path: Path, parent: int, monkeypatch) -> None:
    monkeypatch.setattr(publication, "_rename_noreplace", _emulated_noreplace)
    revision = publish_file(parent, "target", b"binary\0\xff", condition=Create(), create_metadata=_metadata())
    with pytest.raises(FilePublicationError) as raised:
        publish_file(parent, "target", b"overwrite", condition=Create(), create_metadata=_metadata())
    assert raised.value.kind is PublicationFailureKind.CONFLICT
    inventory = inventory_directory(parent, max_entries=4, max_depth=1, max_encoded_bytes=4096, expires_at=None)
    assert [entry.relative_path for entry in inventory] == ["target"]
    assert remove_file_object(parent, "target", expected_kind=FileKind.REGULAR, expected=revision, expires_at=None)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "result,error_number,kind",
    [
        (0, 0, None),
        (-1, errno.EEXIST, PublicationFailureKind.CONFLICT),
        (-1, errno.ENOTSUP, PublicationFailureKind.UNSUPPORTED),
        (-1, errno.EIO, PublicationFailureKind.UNCERTAIN),
    ],
)
def test_create_selects_native_exclusive_rename(monkeypatch, result, error_number, kind) -> None:
    calls = []

    def native(*args):
        calls.append(args)
        ctypes.set_errno(error_number)
        return result

    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(renameatx_np=native))
    if kind is None:
        publication._rename_noreplace(42, "stage", "target")
    else:
        with pytest.raises(FilePublicationError) as raised:
            publication._rename_noreplace(42, "stage", "target")
        assert raised.value.kind is kind
    assert calls == [(42, b"stage", 42, b"target", 4)]


def test_scratch_selection_uses_existing_private_tmp_and_ignores_environment(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "private-tmp"
    selected.mkdir()
    selected.chmod(0o1777)
    seen: list[str] = []
    real_open = paths.open_file_root

    def open_root(path):
        seen.append(path)
        return real_open(str(selected))

    monkeypatch.setattr(scratch_root, "open_file_root", open_root)
    monkeypatch.setattr(scratch_root, "_EXPECTED_OWNER_UID", os.geteuid())
    monkeypatch.setenv("TMPDIR", str(tmp_path / "decoy"))
    descriptor = scratch_root.open_scratch_root(expires_at=None)
    os.close(descriptor)
    assert seen == ["/private/tmp"] and not list(selected.iterdir())
