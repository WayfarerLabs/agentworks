"""Admission of the fixed Linux scratch parent."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._scratch_root as root_module
from agentworks.execution._file_paths import open_linux_root as open_linux_path_root
from agentworks.execution._file_spool import spool_snapshot
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._scratch import cleanup_scratch, iter_ready_scratch
from agentworks.execution._scratch_root import (
    ScratchRootError,
    ScratchRootFailureKind,
    open_scratch_root,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux fixed scratch root")

_TOKEN = bytes(range(16))


@pytest.fixture
def selected_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "selected-scratch"
    root.mkdir()
    root.chmod(0o1777)
    monkeypatch.setattr(root_module, "_LINUX_SCRATCH_ROOT", str(root))
    monkeypatch.setattr(root_module, "_EXPECTED_OWNER_UID", os.geteuid())
    return root


@pytest.fixture
def wrong_owner_root(selected_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(root_module, "_EXPECTED_OWNER_UID", os.geteuid() + 1)
    return selected_root


def _identity() -> IdentityExpectation:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityExpectation(os.geteuid(), os.getegid(), groups)


def _failure(*, expires_at: float | None = None) -> ScratchRootError:
    with pytest.raises(ScratchRootError) as raised:
        open_scratch_root(expires_at=expires_at)
    return raised.value


def test_admission_returns_an_owned_descriptor_without_mutation_or_environment_selection(
    selected_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = selected_root / "retained"
    retained.write_bytes(b"unchanged")
    before = (selected_root.stat().st_mtime_ns, tuple(selected_root.iterdir()))
    decoy = tmp_path / "environment-decoy"
    decoy.mkdir()
    for name in ("TMPDIR", "TMP", "TEMP", "HOME"):
        monkeypatch.setenv(name, str(decoy))

    descriptor = open_scratch_root(expires_at=None)
    try:
        assert os.fstat(descriptor).st_ino == selected_root.stat().st_ino
        assert (selected_root.stat().st_mtime_ns, tuple(selected_root.iterdir())) == before
        assert not tuple(decoy.iterdir())
    finally:
        os.close(descriptor)


def test_missing_root_is_not_created(selected_root: Path) -> None:
    selected_root.rmdir()

    error = _failure()

    assert error.kind is ScratchRootFailureKind.MISSING
    assert not selected_root.exists()


@pytest.mark.parametrize("replacement", ["symlink", "file"])
def test_unsupported_root_objects_are_not_followed(selected_root: Path, replacement: str) -> None:
    selected_root.rmdir()
    if replacement == "symlink":
        target = selected_root.with_name("actual-directory")
        target.mkdir(mode=0o700)
        selected_root.symlink_to(target, target_is_directory=True)
    else:
        selected_root.write_bytes(b"not a directory")

    error = _failure()

    assert error.kind is ScratchRootFailureKind.UNSUPPORTED
    assert error.args == (ScratchRootFailureKind.UNSUPPORTED.value,)
    assert str(selected_root) not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("mode", [0o777, 0o1755, 0o1770, 0o3777])
def test_nonexact_root_mode_is_unsafe(selected_root: Path, mode: int) -> None:
    selected_root.chmod(mode)

    assert _failure().kind is ScratchRootFailureKind.UNSAFE
    assert not tuple(selected_root.iterdir())


def test_wrong_root_owner_is_unsafe(wrong_owner_root: Path) -> None:
    assert _failure().kind is ScratchRootFailureKind.UNSAFE
    assert not tuple(wrong_owner_root.iterdir())


def test_non_linux_host_is_unsupported_before_path_access(selected_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del selected_root
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(root_module, "open_linux_root", lambda _path: pytest.fail("path access attempted"))

    assert _failure().kind is ScratchRootFailureKind.UNSUPPORTED


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "private input"), KeyboardInterrupt()])
def test_post_open_observation_failure_closes_the_owned_descriptor(
    selected_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    del selected_root
    real_fstat = os.fstat
    opened: list[int] = []

    def track_open(path: str) -> int | None:
        descriptor = open_linux_path_root(path)
        assert descriptor is not None
        opened.append(descriptor)
        return descriptor

    def fail_observation(descriptor: int) -> os.stat_result:
        if opened and descriptor == opened[-1]:
            raise failure
        return real_fstat(descriptor)

    monkeypatch.setattr(root_module, "open_linux_root", track_open)
    monkeypatch.setattr(os, "fstat", fail_observation)

    with pytest.raises(ScratchRootError if isinstance(failure, OSError) else KeyboardInterrupt) as raised:
        open_scratch_root(expires_at=None)

    if isinstance(raised.value, ScratchRootError):
        assert raised.value.kind is ScratchRootFailureKind.IO
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        real_fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_missing_observation_cannot_outrun_the_deadline(selected_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected_root.rmdir()
    clock = [0.0]

    def observe_then_expire(path: str) -> int | None:
        descriptor = open_linux_path_root(path)
        assert descriptor is None
        clock[0] = 2.0
        return None

    monkeypatch.setattr(root_module, "open_linux_root", observe_then_expire)
    monkeypatch.setattr(root_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    assert _failure(expires_at=1.0).kind is ScratchRootFailureKind.DEADLINE
    assert not selected_root.exists()


def test_expiry_after_successful_observation_closes_the_owned_descriptor(
    selected_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del selected_root
    clock = [0.0]
    real_fstat = os.fstat
    opened: list[int] = []

    def track_open(path: str) -> int | None:
        descriptor = open_linux_path_root(path)
        assert descriptor is not None
        opened.append(descriptor)
        return descriptor

    def observe_then_expire(descriptor: int) -> os.stat_result:
        observed = real_fstat(descriptor)
        if opened and descriptor == opened[-1]:
            clock[0] = 2.0
        return observed

    monkeypatch.setattr(root_module, "open_linux_root", track_open)
    monkeypatch.setattr(os, "fstat", observe_then_expire)
    monkeypatch.setattr(root_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    assert _failure(expires_at=1.0).kind is ScratchRootFailureKind.DEADLINE
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        real_fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_unsafe_observation_is_closed_before_the_deadline_wins(
    selected_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_root.chmod(0o777)
    clock = [0.0]
    real_fstat = os.fstat
    opened: list[int] = []

    def track_open(path: str) -> int | None:
        descriptor = open_linux_path_root(path)
        assert descriptor is not None
        opened.append(descriptor)
        return descriptor

    def observe_then_expire(descriptor: int) -> os.stat_result:
        observed = real_fstat(descriptor)
        if opened and descriptor == opened[-1]:
            clock[0] = 2.0
        return observed

    monkeypatch.setattr(root_module, "open_linux_root", track_open)
    monkeypatch.setattr(os, "fstat", observe_then_expire)
    monkeypatch.setattr(root_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    assert _failure(expires_at=1.0).kind is ScratchRootFailureKind.DEADLINE
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        real_fstat(opened[0])
    assert closed.value.errno == errno.EBADF


def test_read_only_source_parent_spools_into_selected_scratch_and_cleans_exactly(
    selected_root: Path, tmp_path: Path
) -> None:
    if os.geteuid() == 0:
        pytest.skip("read-only source authority must be tested without root bypass")
    source_root = tmp_path / "read-only-source"
    source_root.mkdir()
    source = source_root / "payload"
    content = b"readable source from a non-writable parent"
    source.write_bytes(content)
    source_root.chmod(0o555)
    retained = selected_root / "retained"
    retained.write_bytes(b"not owned by the transfer")
    source_before = tuple(source_root.iterdir())
    scratch_before = tuple(selected_root.iterdir())
    source_fd = os.open(source_root, os.O_RDONLY | os.O_DIRECTORY)
    scratch_fd = open_scratch_root(expires_at=None)
    try:
        result = spool_snapshot(source_fd, source.name, scratch_fd, len(content), _TOKEN, _identity())
        assert result is not None
        try:
            assert b"".join(iter_ready_scratch(scratch_fd, result.ready)) == content
            assert tuple(source_root.iterdir()) == source_before
            assert set(selected_root.iterdir()) > set(scratch_before)
        finally:
            cleanup_scratch(scratch_fd, result.ready)

        assert tuple(source_root.iterdir()) == source_before
        assert tuple(selected_root.iterdir()) == scratch_before
        assert retained.read_bytes() == b"not owned by the transfer"
    finally:
        os.close(scratch_fd)
        os.close(source_fd)
        source_root.chmod(0o700)
