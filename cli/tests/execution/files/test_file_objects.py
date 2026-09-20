"""Private Linux object observation and conditional removal behavior."""

from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path

import pytest

import agentworks.execution._file_objects as objects_module
from agentworks.execution._file_objects import (
    FileKind,
    FileObjectError,
    FileObjectFailureKind,
    FileObjectPhase,
    remove_file_object,
    revision_kind,
    stat_file_object,
)
from agentworks.execution._file_paths import open_linux_confined
from agentworks.execution._file_snapshot import read_revision
from agentworks.execution._file_stat import FileRevision

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file objects")


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _observe(parent_fd: int, name: str) -> FileRevision:
    revision = stat_file_object(parent_fd, name, expires_at=None)
    assert revision is not None
    return revision


def _failure(function: object, *args: object, **kwargs: object) -> FileObjectError:
    with pytest.raises(FileObjectError) as raised:
        function(*args, **kwargs)  # type: ignore[operator]
    return raised.value


def test_stat_observes_regular_directory_and_socket_without_content(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"private bytes")
    directory = tmp_path / "directory"
    directory.mkdir()
    socket_path = tmp_path / "socket"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    parent_fd = _open_parent(tmp_path)
    try:
        revisions = {
            FileKind.REGULAR: _observe(parent_fd, regular.name),
            FileKind.DIRECTORY: _observe(parent_fd, directory.name),
            FileKind.SOCKET: _observe(parent_fd, socket_path.name),
        }
    finally:
        os.close(parent_fd)
        listener.close()

    for kind, revision in revisions.items():
        target = {FileKind.REGULAR: regular, FileKind.DIRECTORY: directory, FileKind.SOCKET: socket_path}[kind]
        assert revision_kind(revision) is kind
        assert revision.digest is None
        assert revision.stat.inode == target.lstat().st_ino


def test_stat_missing_is_the_only_none_result(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        assert stat_file_object(parent_fd, "missing", expires_at=None) is None
    finally:
        os.close(parent_fd)


def test_non_linux_refuses_before_parent_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    os.close(parent_fd)
    monkeypatch.setattr("agentworks.execution._file_objects.sys.platform", "win32")
    error = _failure(stat_file_object, parent_fd, "target", expires_at=None)
    assert error.kind is FileObjectFailureKind.UNSUPPORTED
    assert error.phase is FileObjectPhase.OBSERVATION


def test_expired_stat_refuses_before_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    monkeypatch.setattr(objects_module, "_open_observed", lambda *_args: pytest.fail("object was observed"))
    try:
        error = _failure(stat_file_object, parent_fd, "target", expires_at=0.0)
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.DEADLINE
    assert error.phase is FileObjectPhase.OBSERVATION


def test_stat_uses_confined_path_only_access_for_unreadable_regular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    target.chmod(0)
    parent_fd = _open_parent(tmp_path)
    observed_flags: list[int] = []

    def recording_open(root_fd: int, path: str, flags: int) -> int | None:
        observed_flags.append(flags)
        return open_linux_confined(root_fd, path, flags)

    monkeypatch.setattr(objects_module, "open_linux_confined", recording_open)
    try:
        revision = _observe(parent_fd, target.name)
    finally:
        os.close(parent_fd)

    assert revision.stat.inode == target.stat().st_ino
    assert observed_flags and observed_flags[-1] & os.O_PATH == os.O_PATH


@pytest.mark.parametrize("object_kind", ["symlink", "hardlink", "fifo"])
def test_stat_refuses_links_and_unknown_special_objects(tmp_path: Path, object_kind: str) -> None:
    target = tmp_path / "target"
    if object_kind == "symlink":
        other = tmp_path / "other"
        other.write_bytes(b"content")
        target.symlink_to(other)
    elif object_kind == "hardlink":
        target.write_bytes(b"content")
        os.link(target, tmp_path / "other")
    else:
        os.mkfifo(target)
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(stat_file_object, parent_fd, target.name, expires_at=None)
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.UNSUPPORTED
    assert error.phase is FileObjectPhase.OBSERVATION


def test_stat_refuses_descendant_mount(tmp_path: Path) -> None:
    if not Path("/proc").is_dir():
        pytest.skip("procfs fixture is unavailable")
    root_fd = _open_parent(Path("/"))
    try:
        error = _failure(stat_file_object, root_fd, "proc", expires_at=None)
    finally:
        os.close(root_fd)
    assert error.kind is FileObjectFailureKind.UNSUPPORTED


def test_stat_detects_named_replacement_during_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    adjacent = tmp_path / "adjacent"
    adjacent.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    original_open = objects_module._open_path

    def replacing_open(parent: int, name: str, phase: FileObjectPhase) -> int | None:
        os.replace(replacement, target)
        return original_open(parent, name, phase)

    monkeypatch.setattr(objects_module, "_open_path", replacing_open)
    try:
        error = _failure(stat_file_object, parent_fd, target.name, expires_at=None)
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.CONFLICT
    assert target.read_bytes() == b"new"
    assert adjacent.read_bytes() == b"keep"


@pytest.mark.parametrize("object_kind", [FileKind.REGULAR, FileKind.DIRECTORY, FileKind.SOCKET])
def test_remove_matching_supported_object(tmp_path: Path, object_kind: FileKind) -> None:
    target = tmp_path / "target"
    listener: socket.socket | None = None
    if object_kind is FileKind.REGULAR:
        target.write_bytes(b"content")
    elif object_kind is FileKind.DIRECTORY:
        target.mkdir()
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(target))
    adjacent = tmp_path / "adjacent"
    adjacent.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _observe(parent_fd, target.name)
        assert remove_file_object(
            parent_fd,
            target.name,
            expected_kind=object_kind,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
        if listener is not None:
            listener.close()
    assert not target.exists()
    assert adjacent.read_bytes() == b"keep"


def test_remove_missing_returns_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _observe(parent_fd, target.name)
        target.unlink()
        assert not remove_file_object(
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)


def test_remove_stat_only_regular_does_not_read_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"unreadable content")
    target.chmod(0)
    parent_fd = _open_parent(tmp_path)
    expected = _observe(parent_fd, target.name)
    monkeypatch.setattr(objects_module, "read_revision", lambda *_args, **_kwargs: pytest.fail("content was read"))
    try:
        assert remove_file_object(
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert not target.exists()


def test_remove_regular_matches_bounded_content_revision(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(os.urandom(3 * 64 * 1024 + 17))
    parent_fd = _open_parent(tmp_path)
    try:
        expected = read_revision(parent_fd, target.name, include_digest=True)
        assert expected is not None
        assert remove_file_object(
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert not target.exists()


def test_remove_refuses_stale_digest_without_deleting(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    parent_fd = _open_parent(tmp_path)
    try:
        observed = _observe(parent_fd, target.name)
        stale = FileRevision(observed.stat, b"\x00" * 32)
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=stale,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.CONFLICT
    assert target.read_bytes() == b"content"


def test_remove_refuses_kind_and_metadata_conflicts(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _observe(parent_fd, target.name)
        kind_error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.DIRECTORY,
            expected=expected,
            expires_at=None,
        )
        target.chmod(0o600)
        metadata_error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert kind_error.kind is FileObjectFailureKind.CONFLICT
    assert metadata_error.kind is FileObjectFailureKind.CONFLICT
    assert target.exists()


def test_remove_refuses_nonempty_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    child = target / "child"
    child.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _observe(parent_fd, target.name)
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.DIRECTORY,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.CONFLICT
    assert child.read_bytes() == b"keep"


def test_remove_refuses_stale_socket_identity(tmp_path: Path) -> None:
    target = tmp_path / "target"
    first = socket.socket(socket.AF_UNIX)
    first.bind(str(target))
    parent_fd = _open_parent(tmp_path)
    second: socket.socket | None = None
    try:
        expected = _observe(parent_fd, target.name)
        first.close()
        target.rename(tmp_path / "old-socket")
        second = socket.socket(socket.AF_UNIX)
        second.bind(str(target))
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.SOCKET,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
        first.close()
        if second is not None:
            second.close()
    assert error.kind is FileObjectFailureKind.CONFLICT
    assert target.exists()


@pytest.mark.parametrize("replacement_kind", ["symlink", "hardlink", "fifo"])
def test_remove_refuses_observed_link_or_special_object(tmp_path: Path, replacement_kind: str) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    adjacent = tmp_path / "adjacent"
    adjacent.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    try:
        expected = _observe(parent_fd, target.name)
        if replacement_kind == "symlink":
            target.unlink()
            target.symlink_to(adjacent)
        elif replacement_kind == "hardlink":
            os.link(target, tmp_path / "other-link")
        else:
            target.unlink()
            os.mkfifo(target)
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.UNSUPPORTED
    assert os.path.lexists(target)
    assert adjacent.read_bytes() == b"keep"


def test_expired_remove_refuses_before_observation_or_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    parent_fd = _open_parent(tmp_path)
    expected = _observe(parent_fd, target.name)
    monkeypatch.setattr(objects_module, "_open_observed", lambda *_args: pytest.fail("object was observed"))
    try:
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=0.0,
        )
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.DEADLINE
    assert target.read_bytes() == b"content"


def test_remove_detects_named_race_before_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"old")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"new")
    adjacent = tmp_path / "adjacent"
    adjacent.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    expected = _observe(parent_fd, target.name)
    original_revalidate = objects_module._revalidate

    def replace_then_revalidate(*args: object, **kwargs: object) -> None:
        os.replace(replacement, target)
        original_revalidate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(objects_module, "_revalidate", replace_then_revalidate)
    try:
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.CONFLICT
    assert target.read_bytes() == b"new"
    assert adjacent.read_bytes() == b"keep"


def test_remove_interruption_after_attempt_carries_uncertainty_without_adjacent_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    adjacent = tmp_path / "adjacent"
    adjacent.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    expected = _observe(parent_fd, target.name)
    original_remove = objects_module._remove_named

    def remove_then_interrupt(parent: int, name: str, kind: FileKind) -> None:
        original_remove(parent, name, kind)
        raise KeyboardInterrupt

    monkeypatch.setattr(objects_module, "_remove_named", remove_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as raised:
        remove_file_object(
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    os.close(parent_fd)
    assert isinstance(raised.value.__cause__, FileObjectError)
    assert raised.value.__cause__.kind is FileObjectFailureKind.UNCERTAIN
    assert raised.value.__cause__.phase is FileObjectPhase.REMOVAL
    assert not target.exists()
    assert adjacent.read_bytes() == b"keep"


def test_remove_lost_acknowledgement_is_uncertain_and_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    adjacent = tmp_path / "adjacent"
    adjacent.write_bytes(b"keep")
    parent_fd = _open_parent(tmp_path)
    expected = _observe(parent_fd, target.name)
    original_unlink = os.unlink

    def unlink_then_fail(path: str, *, dir_fd: int | None = None) -> None:
        original_unlink(path, dir_fd=dir_fd)
        raise OSError(errno.EIO, "fixture lost acknowledgement")

    monkeypatch.setattr(os, "unlink", unlink_then_fail)
    try:
        error = _failure(
            remove_file_object,
            parent_fd,
            target.name,
            expected_kind=FileKind.REGULAR,
            expected=expected,
            expires_at=None,
        )
    finally:
        os.close(parent_fd)
    assert error.kind is FileObjectFailureKind.UNCERTAIN
    assert not target.exists()
    assert adjacent.read_bytes() == b"keep"
