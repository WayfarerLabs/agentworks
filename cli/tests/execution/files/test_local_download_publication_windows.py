"""Native Windows evidence for private local download publication."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from agentworks.execution._local_download_publication_windows import (
    LocalDownloadPartialMutationError,
    WindowsLocalDownloadPublication,
    _WindowsAPI,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.files import Replace

pytestmark = [pytest.mark.windows, pytest.mark.skipif(sys.platform != "win32", reason="native Windows file APIs")]


def _commit(writer: WindowsLocalDownloadPublication, data: bytes) -> None:
    writer.commit(verified_complete=True, size=len(data), sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("data", [b"", b"new\x00bytes"])
def test_create_publishes_verified_stage_without_replacement(tmp_path: Path, data: bytes) -> None:
    destination = tmp_path / "download"
    writer = WindowsLocalDownloadPublication(destination)
    try:
        assert writer.try_write(memoryview(data)) == len(data)
        assert not destination.exists()
        _commit(writer, data)
        assert writer.published
        assert not writer.publication_uncertain
        assert destination.read_bytes() == data
        assert list(tmp_path.iterdir()) == [destination]
    finally:
        writer.abort()


def test_create_refuses_existing_entry_even_if_it_arrives_after_staging(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    writer = WindowsLocalDownloadPublication(destination)
    try:
        writer.try_write(memoryview(b"new"))
        destination.write_bytes(b"old")
        with pytest.raises(OSError):
            _commit(writer, b"new")
        assert destination.read_bytes() == b"old"
        assert not writer.published
    finally:
        writer.abort()
    assert list(tmp_path.iterdir()) == [destination]


def test_held_ancestor_blocks_directory_rename_access(tmp_path: Path) -> None:
    parent = tmp_path / "held"
    parent.mkdir()
    api = _WindowsAPI()
    writer = WindowsLocalDownloadPublication(parent / "download")
    try:
        with pytest.raises(OSError) as blocked:
            api.open(parent, 0x10000, 1 | 2 | 4, 3, 0x02000000 | 0x00200000)
        assert blocked.value.winerror == 32  # ERROR_SHARING_VIOLATION.
    finally:
        writer.abort()
    handle = api.open(parent, 0x10000, 1 | 2 | 4, 3, 0x02000000 | 0x00200000)
    api.close(handle)


@pytest.mark.parametrize("data", [b"", b"n", b"a replacement longer than the old bytes"])
def test_replace_keeps_file_identity_and_access_metadata(tmp_path: Path, data: bytes) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old bytes")
    api = _WindowsAPI()
    handle = api.open(destination, 0x80 | 0x20000, 1 | 2 | 4, 3, 0x00200000)
    try:
        before = api.info(handle)
        security = api.security_descriptor(handle)
    finally:
        api.close(handle)
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(data))
        _commit(writer, data)
        assert writer.published
        assert not writer.publication_uncertain
        assert destination.read_bytes() == data
        handle = api.open(destination, 0x80 | 0x20000, 1 | 2 | 4, 3, 0x00200000)
        try:
            after = api.info(handle)
            assert after.identity == before.identity
            assert after.links == 1
            assert api.security_descriptor(handle) == security
        finally:
            api.close(handle)
    finally:
        writer.abort()


def test_held_replace_target_blocks_another_writer_or_rename(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    api = _WindowsAPI()
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    try:
        with pytest.raises(OSError) as blocked_write:
            api.open(destination, 0x40000000, 1 | 2 | 4, 3, 0x00200000)
        assert blocked_write.value.winerror == 32
        with pytest.raises(OSError) as blocked_delete:
            api.open(destination, 0x10000, 1 | 2 | 4, 3, 0x00200000)
        assert blocked_delete.value.winerror == 32
        assert destination.read_bytes() == b"old"
    finally:
        writer.abort()


def test_replace_rejects_hard_link_and_reparse_destination(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.write_bytes(b"old")
    hardlink = tmp_path / "hardlink"
    os.link(ordinary, hardlink)
    with pytest.raises(OSError):
        WindowsLocalDownloadPublication(hardlink, condition=Replace())
    assert hardlink.read_bytes() == b"old"
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(ordinary)
    except OSError:
        pytest.skip("Windows symlink creation is unavailable to this account")
    with pytest.raises(OSError):
        WindowsLocalDownloadPublication(alias, condition=Replace())
    assert ordinary.read_bytes() == b"old"


def test_replace_reports_partial_bytes_after_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old content")
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    original_write = writer._api.write
    failed = False

    def partial_then_fail(handle: int, data: memoryview) -> int:
        nonlocal failed
        if handle == writer._target:
            if failed:
                raise OSError("injected destination write failure")
            failed = True
            return original_write(handle, data[:2])
        return original_write(handle, data)

    monkeypatch.setattr(writer._api, "write", partial_then_fail)
    try:
        writer.try_write(memoryview(b"new bytes"))
        with pytest.raises(LocalDownloadPartialMutationError):
            _commit(writer, b"new bytes")
        assert writer.possible_local_change
        assert writer.publication_uncertain
        assert not writer.published
        assert destination.read_bytes().startswith(b"ne")
    finally:
        writer.abort()
    assert destination.exists()


def test_unverified_replace_does_not_mutate_destination(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    try:
        writer.try_write(memoryview(b"new"))
        with pytest.raises(ValueError):
            writer.commit(verified_complete=False, size=3, sha256=hashlib.sha256(b"new").hexdigest())
        assert destination.read_bytes() == b"old"
        assert not writer.possible_local_change
    finally:
        writer.abort()


def test_expired_deadline_refuses_before_replace_mutation(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
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
        assert not writer.possible_local_change
    finally:
        writer.abort()


def test_readonly_destination_refuses_before_staging(tmp_path: Path) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    destination.chmod(0o444)
    try:
        with pytest.raises(OSError):
            WindowsLocalDownloadPublication(destination, condition=Replace())
        assert destination.read_bytes() == b"old"
        assert not list(tmp_path.glob(".agw-download-*"))
    finally:
        destination.chmod(0o666)


def test_rejects_reparse_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink creation is unavailable to this account")
    with pytest.raises(OSError):
        WindowsLocalDownloadPublication(alias / "download")
    assert not list(actual.iterdir())
