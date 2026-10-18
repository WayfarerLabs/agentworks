"""Directory custody on Windows and POSIX, without VM or operator resources."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.errors import StateError
from agentworks.execution import _lima_selection
from agentworks.execution._lima_selection import LimaDirectoryControlFact, LimaDirectoryCustody
from agentworks.execution._local_download_publication_windows import (
    _BACKUP_SEMANTICS,
    _DIRECTORY,
    _LIST_DIRECTORY,
    _OPEN_EXISTING,
    _READ_ATTRIBUTES,
    _SHARE_DELETE,
    _SHARE_READ,
    _SHARE_WRITE,
    _Identity,
    _Observed,
)
from agentworks.execution.carrier import Deadline
from tests.vms.test_lima_native_access import local as local

pytestmark = pytest.mark.windows


class FakeWindowsAPI:
    def __init__(self, volume: int, inode: int) -> None:
        self.observed = _Observed(_Identity(volume, inode.to_bytes(16, "little")), _DIRECTORY, 1, 0, (0, 0), (0, 0))
        self.opened: list[tuple[object, ...]] = []
        self.closed: list[int] = []
        self.info_error: BaseException | None = None
        self.close_error: BaseException | None = None

    def open(self, *args) -> int:
        self.opened.append(args)
        return 77

    def info(self, handle: int) -> _Observed:
        assert handle == 77
        if self.info_error is not None:
            raise self.info_error
        return self.observed

    def close(self, handle: int) -> None:
        self.closed.append(handle)
        if self.close_error is not None:
            raise self.close_error


def windows(monkeypatch, api: FakeWindowsAPI, volume: int, inode: int) -> None:
    monkeypatch.setattr(_lima_selection, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(_lima_selection, "_windows_directory_api", lambda: api)
    monkeypatch.setattr(
        _lima_selection,
        "os",
        SimpleNamespace(
            **dict(vars(os), stat=lambda path: SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=volume, st_ino=inode))
        ),
    )


def test_windows_held_identity_uses_full_file_id_and_allows_name_replacement(tmp_path, monkeypatch) -> None:
    volume, inode = 2**60 + 3, 2**120 + 7
    api = FakeWindowsAPI(volume, inode)
    windows(monkeypatch, api, volume, inode)
    held = LimaDirectoryCustody.open(str(tmp_path))
    assert held.identity.device == volume and held.identity.inode == inode
    assert api.opened == [
        (
            tmp_path,
            _READ_ATTRIBUTES | _LIST_DIRECTORY,
            _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE,
            _OPEN_EXISTING,
            _BACKUP_SEMANTICS,
        )
    ]
    held.check()
    windows(monkeypatch, api, volume, inode + 1)
    with pytest.raises(StateError):
        held.check()
    assert held.close() and held.settled and api.closed == [77]


def test_interrupted_native_handle_close_is_retained_and_never_retried(tmp_path, monkeypatch) -> None:
    api = FakeWindowsAPI(5, 2**90)
    windows(monkeypatch, api, 5, 2**90)
    held = LimaDirectoryCustody.open(str(tmp_path))
    control = KeyboardInterrupt()
    api.close_error = control
    with pytest.raises(KeyboardInterrupt) as caught:
        held.close()
    assert caught.value is control
    assert not held.close() and not held.settled and held.descriptor == 77
    assert api.closed == [77]


def test_failed_full_id_inspection_closes_only_the_original_handle(tmp_path, monkeypatch) -> None:
    api = FakeWindowsAPI(5, 17)
    windows(monkeypatch, api, 5, 17)
    control = OSError("full identity API unavailable")
    api.info_error = control
    with pytest.raises(OSError) as caught:
        LimaDirectoryCustody.open(str(tmp_path))
    assert caught.value is control and api.closed == [77]


def test_interrupted_construction_cleanup_carries_actual_handle_owner(tmp_path, monkeypatch) -> None:
    api = FakeWindowsAPI(5, 17)
    windows(monkeypatch, api, 5, 17)
    control = OSError("full identity API unavailable")
    api.info_error = control
    api.close_error = KeyboardInterrupt()
    with pytest.raises(OSError) as caught:
        LimaDirectoryCustody.open(str(tmp_path))
    assert caught.value is control
    fact = control.__cause__
    assert isinstance(fact, LimaDirectoryControlFact)
    assert fact.custody.descriptor == 77 and not fact.custody.close()
    assert api.closed == [77]


@pytest.mark.skipif(sys.platform == "win32", reason="synthetic Linux Lima query fixture")
def test_access_retains_directory_construction_uncertainty_even_when_query_settled(local, monkeypatch) -> None:
    fake, access = local
    observed = os.stat(fake.directory)
    api = FakeWindowsAPI(observed.st_dev, observed.st_ino)
    windows(monkeypatch, api, observed.st_dev, observed.st_ino)
    api.info_error = StateError("full identity API unavailable")
    api.close_error = KeyboardInterrupt()
    with pytest.raises(StateError):
        access.observe_power(Deadline.after(5))
    assert access.custody.settled and access.route.directory.descriptor == 77
    assert not access.settle(Deadline.after(5))
    assert api.closed == [77]


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Win32 directory handles")
def test_native_windows_held_full_identity_matches_python_stat_and_replacement(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    held = LimaDirectoryCustody.open(str(selected))
    try:
        observed = os.stat(selected)
        assert (held.identity.device, held.identity.inode) == (observed.st_dev, observed.st_ino)
        held.check()
        selected.rename(tmp_path / "original")
        selected.mkdir()
        with pytest.raises(StateError):
            held.check()
    finally:
        assert held.close() and held.settled
