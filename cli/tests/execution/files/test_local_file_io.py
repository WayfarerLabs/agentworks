"""Real workstation owner behavior; native Windows coverage selects this module."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path, PurePosixPath

import pytest

from agentworks.execution import _local_file_io as local
from agentworks.execution import _local_file_io_platform as platform
from agentworks.execution._local_file_io_types import LocalFileCleanupUncertainError
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Change, FileKind

pytestmark = pytest.mark.windows


def _inventory(root: Path, *, entries: int = 30, depth: int = 8) -> tuple[local.LocalTreeEntry, ...]:
    owner = local.LocalTreeInventory(root, max_entries=entries, max_depth=depth)
    deadline = Deadline.after(10)
    try:
        return owner.inventory(deadline)
    finally:
        owner.close(deadline)
        assert owner.settled


@pytest.mark.parametrize("data", [b"", b"abc\x00\xff", b"x" * (1024 * 1024 + 3)], ids=["empty", "binary", "chunked"])
def test_source_reads_exact_size_and_verified_eof(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "source"
    path.write_bytes(data)
    owner = local.LocalFileSource(path)
    deadline = Deadline.after(10)
    try:
        assert owner.settled
        assert owner.open(deadline) == len(data)
        assert not owner.settled
        chunks = []
        while chunk := owner.read(99_001):
            assert len(chunk) <= 99_001
            chunks.append(chunk)
        assert b"".join(chunks) == data
        assert owner.read(1) == b""
    finally:
        owner.close(deadline)
    assert owner.settled
    with pytest.raises(ValueError):
        owner.read(1)
    with pytest.raises(ValueError):
        owner.open(deadline)


def test_constructors_do_no_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Constructor performed filesystem I/O")

    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "mkdir", forbidden)
    owners = (
        local.LocalFileSource(tmp_path / "missing"),
        local.LocalDestination(tmp_path / "missing"),
        local.LocalTreeInventory(tmp_path / "missing", max_entries=1, max_depth=0),
    )
    for owner in owners:
        assert owner.settled
        owner.close(Deadline.after(0))


@pytest.mark.parametrize("change", ["longer", "shorter", "same-size", "rename"])
def test_source_refuses_changed_held_or_named_source(tmp_path: Path, change: str) -> None:
    if sys.platform == "win32" and change == "rename":
        pytest.skip("The native non-delete-shared leaf refuses rename itself")
    path = tmp_path / "source"
    path.write_bytes(b"abcd")
    source = local.LocalFileSource(path)
    deadline = Deadline.after(10)
    try:
        assert source.open(deadline) == 4
        if change == "rename":
            path.rename(tmp_path / "moved")
            path.write_bytes(b"abcd")
        else:
            path.write_bytes({"longer": b"abcde", "shorter": b"a", "same-size": b"wxyz"}[change])
            if change == "same-size":
                observed = path.stat()
                os.utime(path, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000_000))
        with pytest.raises(FileExistsError):
            while source.read(2):
                pass
    finally:
        source.close(deadline)


def test_source_expected_inventory_identity_is_compared(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"a")
    entry = _inventory(tmp_path)[1]
    source.write_bytes(b"changed")
    owner = local.LocalFileSource(source, expected=entry.identity)
    deadline = Deadline.after(10)
    try:
        with pytest.raises(FileExistsError):
            owner.open(deadline)
    finally:
        owner.close(deadline)
    assert owner.settled


def test_inventory_keeps_empty_entries_root_and_deterministic_order(tmp_path: Path) -> None:
    (tmp_path / "z").write_bytes(b"123")
    (tmp_path / "empty").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "\u03bb").write_bytes(b"")
    entries = _inventory(tmp_path, entries=5, depth=2)
    assert [(entry.relative_path, entry.kind, entry.size) for entry in entries] == [
        (PurePosixPath("."), FileKind.DIRECTORY, 0),
        (PurePosixPath("a"), FileKind.DIRECTORY, 0),
        (PurePosixPath("a/\u03bb"), FileKind.REGULAR, 0),
        (PurePosixPath("empty"), FileKind.DIRECTORY, 0),
        (PurePosixPath("z"), FileKind.REGULAR, 3),
    ]
    assert "source" not in repr(entries[0].identity)
    assert str(tmp_path) not in repr(entries)


def test_inventory_zero_bounds_and_complete_depth_boundary(tmp_path: Path) -> None:
    assert len(_inventory(tmp_path, entries=1, depth=0)) == 1
    (tmp_path / "empty").mkdir()
    assert len(_inventory(tmp_path, entries=2, depth=1)) == 2
    (tmp_path / "empty" / "too-deep").write_bytes(b"")
    with pytest.raises(local.LocalFileLimitError):
        _inventory(tmp_path, entries=3, depth=1)
    with pytest.raises(local.LocalFileLimitError):
        _inventory(tmp_path, entries=2, depth=8)
    (tmp_path / "empty" / "too-deep").write_bytes(b"x")
    assert sum(entry.size for entry in _inventory(tmp_path, entries=3, depth=8)) == 1


@pytest.mark.parametrize("limits", [(0, 0), (4097, 0), (1, 9), (1, -1)])
def test_invalid_limits_refuse_before_io(tmp_path: Path, limits: tuple[int, int]) -> None:
    with pytest.raises(ValueError):
        local.LocalTreeInventory(tmp_path, max_entries=limits[0], max_depth=limits[1])


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory-link"])
def test_inventory_and_source_refuse_links_and_specials(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source"
    other = tmp_path / "other"
    other.write_bytes(b"content")
    if kind == "hardlink":
        os.link(other, source)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("This host has no FIFO creation primitive")
        os.mkfifo(source)
    else:
        try:
            source.symlink_to(root if kind == "directory-link" else other, target_is_directory=kind == "directory-link")
        except OSError:
            pytest.skip("This account cannot create symlinks")
    with pytest.raises(OSError):
        _inventory(root)
    owner = local.LocalFileSource(source)
    deadline = Deadline.after(10)
    try:
        with pytest.raises(OSError):
            owner.open(deadline)
    finally:
        owner.close(deadline)


def test_directory_is_one_level_private_and_existing_metadata_unchanged(tmp_path: Path) -> None:
    destination = tmp_path / "new"
    owner = local.LocalDestination(destination)
    deadline = Deadline.after(10)
    try:
        assert owner.ensure_directory(deadline) is Change.CHANGED
        assert owner.changed and not owner.mutation_uncertain
        if sys.platform != "win32":
            assert stat.S_IMODE(destination.stat().st_mode) == 0o700
            destination.chmod(0o750)
    finally:
        owner.close(deadline)
    before = destination.stat()
    owner = local.LocalDestination(destination)
    try:
        assert owner.ensure_directory(deadline) is Change.UNCHANGED
        assert not owner.changed and not owner.mutation_uncertain
    finally:
        owner.close(deadline)
    assert destination.stat().st_mode == before.st_mode
    missing_parent = local.LocalDestination(tmp_path / "missing" / "child")
    try:
        with pytest.raises(FileNotFoundError):
            missing_parent.ensure_directory(deadline)
    finally:
        missing_parent.close(deadline)
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt, SystemExit])
def test_creation_control_retains_effect_and_never_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: type[BaseException],
) -> None:
    owner = local.LocalDestination(tmp_path / "new")
    actual_mkdir = owner._handles.mkdir
    calls = 0
    original = control()

    def create_then_stop(parent: platform._HeldHandle, name: str) -> None:
        nonlocal calls
        calls += 1
        actual_mkdir(parent, name)
        raise original

    monkeypatch.setattr(owner._handles, "mkdir", create_then_stop)
    deadline = Deadline.after(10)
    with pytest.raises(control) as stopped:
        owner.ensure_directory(deadline)
    assert stopped.value is original
    assert owner.mutation_uncertain and not owner.changed
    owner.close(deadline)
    owner.close(deadline)
    assert calls == 1 and owner.settled and (tmp_path / "new").is_dir()


def test_racing_creator_conflicts_without_convergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "new"
    owner = local.LocalDestination(destination)
    actual_mkdir = owner._handles.mkdir

    def race(parent: platform._HeldHandle, name: str) -> None:
        destination.mkdir(mode=0o755)
        actual_mkdir(parent, name)

    monkeypatch.setattr(owner._handles, "mkdir", race)
    deadline = Deadline.after(10)
    try:
        with pytest.raises(FileExistsError):
            owner.ensure_directory(deadline)
        assert not owner.changed and not owner.mutation_uncertain
    finally:
        owner.close(deadline)


def test_expired_deadline_prevents_io_but_never_prevents_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"a")
    owner = local.LocalFileSource(path)
    owner.open(Deadline.after(10))
    owner.close(Deadline.after(0))
    assert owner.settled
    directory = local.LocalDestination(tmp_path / "missing")
    try:
        with pytest.raises(TimeoutError):
            directory.ensure_directory(Deadline.after(0))
    finally:
        directory.close(Deadline.after(0))
    assert not (tmp_path / "missing").exists()


def test_destination_metadata_probe_reports_existing_and_missing_objects(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"private")
    deadline = Deadline.after(10)
    probe = local.LocalDestination(path)
    try:
        entry = probe.observe(deadline)
        assert entry is not None and entry.kind is FileKind.REGULAR and entry.size == 7
        assert probe.identity is entry.identity
        assert entry.relative_path == PurePosixPath(".")
        with pytest.raises(local.LocalFileUnsupportedError):
            probe.ensure_directory(deadline)
    finally:
        probe.close(deadline)
    probe = local.LocalDestination(tmp_path / "missing")
    try:
        assert probe.observe(deadline) is None
        assert probe.identity is None
        assert not (tmp_path / "missing").exists()
    finally:
        probe.close(deadline)


def test_destination_probe_detects_a_race_before_directory_creation(tmp_path: Path) -> None:
    path = tmp_path / "missing"
    probe = local.LocalDestination(path)
    deadline = Deadline.after(10)
    try:
        assert probe.observe(deadline) is None
        path.mkdir()
        with pytest.raises(FileExistsError):
            probe.ensure_directory(deadline)
        assert not probe.changed and not probe.mutation_uncertain
    finally:
        probe.close(deadline)


def test_complete_inventory_at_maximum_entry_bound(tmp_path: Path) -> None:
    for index in range(4_095):
        (tmp_path / str(index)).touch()
    entries = _inventory(tmp_path, entries=4_096, depth=1)
    assert len(entries) == 4_096
    assert sum(entry.size for entry in entries) == 0
    (tmp_path / "overflow").touch()
    with pytest.raises(local.LocalFileLimitError):
        _inventory(tmp_path, entries=4_096, depth=1)


def test_inventory_visits_depth_eight_directory_instead_of_omitting_children(tmp_path: Path) -> None:
    current = tmp_path
    for _ in range(8):
        current = current / "child"
        current.mkdir()
    assert len(_inventory(tmp_path, entries=10, depth=8)) == 9
    (current / "deeper").touch()
    with pytest.raises(local.LocalFileLimitError):
        _inventory(tmp_path, entries=10, depth=8)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux search-only ancestry uses O_PATH")
def test_source_and_destination_probe_admit_search_only_parent(tmp_path: Path) -> None:
    parent = tmp_path / "search-only"
    parent.mkdir()
    path = parent / "file"
    path.write_bytes(b"a")
    parent.chmod(0o100)
    deadline = Deadline.after(10)
    source, probe = local.LocalFileSource(path), local.LocalDestination(path)
    try:
        assert source.open(deadline) == 1
        assert source.read(1) == b"a" and source.read(1) == b""
        assert probe.observe(deadline) is not None
    finally:
        source.close(deadline)
        probe.close(deadline)
        parent.chmod(0o700)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor reuse fault seam")
@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt, SystemExit])
def test_uncertain_close_is_never_retried_and_other_originals_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: type[BaseException],
) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"a")
    owner = local.LocalFileSource(path)
    deadline = Deadline.after(10)
    owner.open(deadline)
    assert owner._file is not None
    target = owner._file.token
    real_close = os.close
    calls: list[int] = []
    original = control()

    def close_then_stop(fd: int) -> None:
        calls.append(fd)
        real_close(fd)
        if fd == target:
            raise original

    monkeypatch.setattr(os, "close", close_then_stop)
    with pytest.raises(control) as stopped:
        owner.close(deadline)
    assert stopped.value is original
    assert not owner.settled
    assert len(owner._handles.handles) == 1
    with pytest.raises(LocalFileCleanupUncertainError):
        owner.close(deadline)
    assert calls.count(target) == 1
