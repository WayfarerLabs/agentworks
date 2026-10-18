"""Deterministic host seams; these do not replace native Windows/macOS proof."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.execution import _local_file_io as local
from agentworks.execution import _local_file_io_platform as platform
from agentworks.execution._local_download_publication_windows import _WindowsAPI
from agentworks.execution._local_file_io_types import (
    _validate_windows_component,
    _validate_windows_path,
    validate_local_tree_component,
    validate_local_tree_destination,
)
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from os import _ScandirIterator


@pytest.mark.parametrize(
    "name",
    [
        "NUL",
        "con.txt",
        "LPT1.log",
        "COM\u00b9",
        "trailing.",
        "trailing ",
        "ads:stream",
        "a\\b",
        "a/b",
        "CONIN$",
        "bad\x00",
        "..",
        "",
        "x?",
    ],
)
def test_windows_component_validation_refuses_ambiguous_names(name: str) -> None:
    with pytest.raises(local.LocalFileUnsupportedError):
        _validate_windows_component(name)


@pytest.mark.parametrize("name", ["file", "normal.txt", "\u03bb", ".hidden", "COM0", "company"])
def test_windows_component_validation_accepts_ordinary_names(name: str) -> None:
    _validate_windows_component(name)


@pytest.mark.parametrize(
    "path",
    [
        "\\\\host\\share\\file",
        "\\\\?\\C:\\file",
        "\\\\.\\NUL",
        "C:relative",
        "C:\\CON\\file",
        "C:\\dir.\\file",
        "C:\\dir\\ads:stream",
    ],
)
def test_windows_validates_all_ancestors_and_leaf(path: str) -> None:
    with pytest.raises(local.LocalFileUnsupportedError):
        _validate_windows_path(PureWindowsPath(path))


def test_windows_rejects_overlong_utf16_and_unencodable_paths() -> None:
    for name in ("\U0001f642" * 130, "bad\ud800"):
        with pytest.raises(local.LocalFileUnsupportedError):
            _validate_windows_path(PureWindowsPath("C:/") / name)


def test_pure_host_validation_rejects_unnormalized_path_and_non_utf8_component(tmp_path: Path) -> None:
    with pytest.raises(local.LocalFileUnsupportedError):
        validate_local_tree_destination(tmp_path / ".." / "file")
    with pytest.raises(local.LocalFileUnsupportedError):
        validate_local_tree_component("bad\ud800")


def test_windows_read_only_primitives_and_retained_non_delete_shared_handles(tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []
    identity = SimpleNamespace(volume=1, file_id=b"full identity")
    observed = SimpleNamespace(identity=identity, attributes=0x20, links=1, size=3, created=(1, 2), modified=(3, 4))

    class API:
        def open(self, path: Path, access: int, sharing: int, disposition: int, flags: int) -> int:
            calls.append((path, access, sharing, disposition, flags))
            return len(calls)

        def info(self, token: int) -> SimpleNamespace:
            return observed

        def read(self, token: int) -> bytes:
            return b"abc"

        def close(self, token: int) -> None:
            calls.append(("close", token))

    handles = platform.WindowsHandles()
    handles._api = cast("_WindowsAPI", API())
    held = handles._open(tmp_path / "source", directory=False, deadline=Deadline.after(10))
    assert calls[0][1:4] == (0x80000000, 1 | 2, 3)
    assert handles.observe(held).inode == identity.file_id
    assert handles.read(held, 1) == b"a"
    assert handles.read(held, 2) == b"bc"
    observed.attributes = 0x400
    with pytest.raises(local.LocalFileUnsupportedError):
        handles.observe(held)
    observed.attributes = 0x20
    observed.links = 2
    with pytest.raises(local.LocalFileUnsupportedError):
        handles.observe(held)
    handles.close()
    assert handles.settled


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fault seams")
@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_posix_owners_work_without_linux_only_open_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system: str):
    monkeypatch.setattr(sys, "platform", system)
    if system == "darwin":
        monkeypatch.setattr(platform.PosixHandles, "_observation_flags", staticmethod(lambda: os.O_PATH))
    path = tmp_path / "source"
    path.write_bytes(b"a")
    owner = local.LocalFileSource(path)
    deadline = Deadline.after(10)
    try:
        assert owner.open(deadline) == 1
        assert owner.read(1) == b"a"
        assert owner.read(1) == b""
    finally:
        owner.close(deadline)
    assert owner.settled


@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt, SystemExit])
def test_read_control_retains_originals_for_cleanup_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: type[BaseException],
) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"a")
    source = local.LocalFileSource(path)
    deadline = Deadline.after(10)
    source.open(deadline)
    original = control()
    calls = 0

    def stop(held: platform._HeldHandle, maximum: int) -> bytes:
        nonlocal calls
        calls += 1
        raise original

    monkeypatch.setattr(source._handles, "read", stop)
    with pytest.raises(control) as caught:
        source.read(1)
    assert caught.value is original and not source.settled
    source.close(deadline)
    source.close(deadline)
    assert source.settled and calls == 1


def test_extra_byte_is_checked_after_the_declared_size(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"a")
    source = local.LocalFileSource(path)
    deadline = Deadline.after(10)
    try:
        assert source.open(deadline) == 1
        assert source.read(1) == b"a"
        with path.open("ab") as stream:
            stream.write(b"b")
        with pytest.raises(FileExistsError):
            source.read(1)
    finally:
        source.close(deadline)


def test_inventory_refuses_mount_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "file").write_bytes(b"a")
    owner = local.LocalTreeInventory(tmp_path, max_entries=2, max_depth=1)
    real_stat = owner._handles.stat_child

    def mount(parent: platform._HeldHandle, name: str, deadline: Deadline) -> local.LocalIdentity:
        observed = real_stat(parent, name, deadline)
        return replace(observed, device=observed.device + 1)

    monkeypatch.setattr(owner._handles, "stat_child", mount)
    deadline = Deadline.after(10)
    try:
        with pytest.raises(local.LocalFileUnsupportedError):
            owner.inventory(deadline)
    finally:
        owner.close(deadline)
    assert owner.settled


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX retained-open fault seam")
def test_open_failure_retains_acquired_ancestors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "source").write_bytes(b"a")
    owner = local.LocalFileSource(tmp_path / "source")
    actual_open = os.open
    original = KeyboardInterrupt()

    def fail_leaf(path: object, *args: object, **kwargs: object) -> int:
        if path == "source":
            raise original
        return actual_open(path, *args, **kwargs)  # type: ignore[arg-type,call-overload]

    monkeypatch.setattr(os, "open", fail_leaf)
    deadline = Deadline.after(10)
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.open(deadline)
    assert caught.value is original and not owner.settled
    owner.close(deadline)
    assert owner.settled


def test_object_key_is_stable_across_metadata_changes(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"a")
    owner = local.LocalDestination(path)
    deadline = Deadline.after(10)
    try:
        entry = owner.observe(deadline)
        assert entry is not None
        changed = replace(entry.identity, size=100, times=(0,), mode=0)
        assert changed != entry.identity
        assert changed.object_key == entry.identity.object_key
    finally:
        owner.close(deadline)


@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt, SystemExit])
def test_scan_control_retains_actual_iterator_and_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: type[BaseException],
) -> None:
    (tmp_path / "file").touch()
    owner = local.LocalTreeInventory(tmp_path, max_entries=2, max_depth=1)
    real_scan = owner._handles.scan
    original = control()

    def scan_then_stop(held: platform._HeldHandle) -> platform._HeldScan:
        real_scan(held)
        raise original

    monkeypatch.setattr(owner._handles, "scan", scan_then_stop)
    deadline = Deadline.after(10)
    with pytest.raises(control) as caught:
        owner.inventory(deadline)
    assert caught.value is original and owner._handles.scans and not owner.settled
    owner.close(deadline)
    assert owner.settled


def test_directory_reduction_failure_keeps_confirmed_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = local.LocalDestination(tmp_path / "new")
    original = KeyboardInterrupt()

    def stop(held: platform._HeldHandle) -> None:
        raise original

    monkeypatch.setattr(owner._handles, "restrict_directory", stop)
    deadline = Deadline.after(10)
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.ensure_directory(deadline)
    assert caught.value is original and owner.changed and not owner.mutation_uncertain
    owner.close(deadline)
    assert owner.settled and (tmp_path / "new").is_dir()


@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt, SystemExit])
def test_windows_unknown_handle_close_retains_only_uncertain_original(
    tmp_path: Path,
    control: type[BaseException],
) -> None:
    calls: list[int] = []
    original = control()

    class API:
        def close(self, token: int) -> None:
            calls.append(token)
            if token == 1:
                raise original

    handles = platform.WindowsHandles()
    handles._api = cast("_WindowsAPI", API())
    first, second = platform._HeldHandle(1, tmp_path), platform._HeldHandle(2, tmp_path)
    handles.handles.extend((first, second))
    with pytest.raises(control) as caught:
        handles.close()
    assert caught.value is original
    assert handles.handles == [first] and first.uncertain and not handles.settled
    with pytest.raises(local.LocalFileCleanupUncertainError):
        handles.close()
    assert calls == [2, 1]


@pytest.mark.parametrize("control", [OSError, KeyboardInterrupt, SystemExit])
def test_uncertain_iterator_close_is_not_retried(
    tmp_path: Path,
    control: type[BaseException],
) -> None:
    calls = 0
    original = control()
    handles = platform.PosixHandles()
    actual = os.scandir(tmp_path)

    class Scan:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            actual.close()
            raise original

    held = platform._HeldScan(cast("_ScandirIterator[str]", Scan()))
    handles.scans.append(held)
    with pytest.raises(control) as caught:
        handles.close()
    assert caught.value is original and not handles.settled
    with pytest.raises(local.LocalFileCleanupUncertainError):
        handles.close()
    assert calls == 1
