"""Native Windows evidence for private local download publication."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from agentworks.execution._local_download_publication import LocalDownloadCleanupUncertainError
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


def test_held_ancestor_blocks_directory_rename_and_delete_access(tmp_path: Path) -> None:
    parent = tmp_path / "held"
    parent.mkdir()
    renamed = tmp_path / "renamed"
    api = _WindowsAPI()
    writer = WindowsLocalDownloadPublication(parent / "download")
    unexpected_handle: int | None = None
    try:
        with pytest.raises(OSError) as delete_blocked:
            unexpected_handle = api.open(parent, 0x10000, 1 | 2 | 4, 3, 0x02000000 | 0x00200000)
        assert cast("Any", delete_blocked.value).winerror == 32
        with pytest.raises(OSError) as blocked:
            parent.rename(renamed)
        assert cast("Any", blocked.value).winerror == 32  # ERROR_SHARING_VIOLATION.
    finally:
        if unexpected_handle is not None:
            api.close(unexpected_handle)
        writer.abort()
    parent.rename(renamed)
    assert renamed.is_dir() and not parent.exists()


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
        assert cast("Any", blocked_write.value).winerror == 32
        with pytest.raises(OSError) as blocked_delete:
            api.open(destination, 0x10000, 1 | 2 | 4, 3, 0x00200000)
        assert cast("Any", blocked_delete.value).winerror == 32
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


@pytest.mark.parametrize("stop", [KeyboardInterrupt, SystemExit])
def test_replace_preserves_exceptional_control_after_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: type[BaseException]
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old content")
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    original_write = writer._api.write
    wrote_target = False

    def partial_then_stop(handle: int, data: memoryview) -> int:
        nonlocal wrote_target
        if handle == writer._target:
            if wrote_target:
                raise stop("injected exceptional control")
            wrote_target = True
            return original_write(handle, data[:2])
        return original_write(handle, data)

    monkeypatch.setattr(writer._api, "write", partial_then_stop)
    try:
        writer.try_write(memoryview(b"new bytes"))
        with pytest.raises(stop) as stopped:
            _commit(writer, b"new bytes")
        assert type(stopped.value) is stop
        assert writer.possible_local_change and writer.publication_uncertain
        assert not writer.published
        assert destination.read_bytes().startswith(b"ne")
    finally:
        writer.abort()


def test_replace_close_failure_keeps_change_uncertain_and_cleans_other_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new"))
    target = writer._target
    real_close = writer._api.close
    failed = False

    def close_then_interrupt(handle: int) -> None:
        nonlocal failed
        real_close(handle)
        if handle == target and not failed:
            failed = True
            raise OSError("injected interruption after target close admission")

    monkeypatch.setattr(writer._api, "close", close_then_interrupt)
    with pytest.raises(LocalDownloadPartialMutationError):
        _commit(writer, b"new")
    assert writer.possible_local_change
    assert writer.publication_uncertain
    assert not writer.published
    assert writer.cleanup_uncertain
    with pytest.raises(LocalDownloadCleanupUncertainError):
        writer.abort()
    assert writer._stage is None
    assert not writer._ancestors
    assert not list(tmp_path.glob(".agw-download-*"))
    assert destination.read_bytes() == b"new"


def test_stage_close_uncertainty_does_not_block_ancestor_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = WindowsLocalDownloadPublication(tmp_path / "download")
    stage = writer._stage
    real_close = writer._api.close
    failed = False

    def close_then_interrupt(handle: int) -> None:
        nonlocal failed
        real_close(handle)
        if handle == stage and not failed:
            failed = True
            raise OSError("injected interruption after stage close admission")

    monkeypatch.setattr(writer._api, "close", close_then_interrupt)
    with pytest.raises(LocalDownloadCleanupUncertainError):
        writer.abort()
    assert writer.cleanup_uncertain
    assert not writer._ancestors
    assert not list(tmp_path.glob(".agw-download-*"))
    with pytest.raises(LocalDownloadCleanupUncertainError):
        writer.abort()


def test_ancestor_close_uncertainty_does_not_block_other_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = WindowsLocalDownloadPublication(tmp_path / "download")
    assert len(writer._ancestors) > 1
    first = writer._ancestors[-1]
    real_close = writer._api.close
    failed = False

    def close_then_interrupt(handle: int) -> None:
        nonlocal failed
        real_close(handle)
        if handle == first and not failed:
            failed = True
            raise OSError("injected interruption after ancestor close admission")

    monkeypatch.setattr(writer._api, "close", close_then_interrupt)
    with pytest.raises(LocalDownloadCleanupUncertainError):
        writer.abort()
    assert not writer._ancestors
    assert writer.cleanup_uncertain
    assert not list(tmp_path.glob(".agw-download-*"))


def test_later_observation_close_does_not_erase_earlier_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = WindowsLocalDownloadPublication(tmp_path / "download")
    assert writer._stage_path is not None
    first = writer._open_observation(writer._stage_path)
    assert first is not None
    real_close = writer._api.close
    interrupted = False

    def close_then_interrupt(handle: int) -> None:
        nonlocal interrupted
        real_close(handle)
        if handle == first and not interrupted:
            interrupted = True
            raise OSError("injected interruption after observation close admission")

    monkeypatch.setattr(writer._api, "close", close_then_interrupt)
    with pytest.raises(OSError):
        writer._close_observation(first)
    assert writer.cleanup_uncertain
    second = writer._open_observation(writer._stage_path)
    assert second is not None
    writer._close_observation(second)
    assert writer.cleanup_uncertain
    with pytest.raises(LocalDownloadCleanupUncertainError):
        writer.abort()
    assert not writer._ancestors
    assert not list(tmp_path.glob(".agw-download-*"))


@pytest.mark.parametrize("phase", ["truncate", "flush", "metadata", "close"])
def test_replace_checks_deadline_after_each_final_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    class MutableDeadline:
        expired = False

    destination = tmp_path / "download"
    destination.write_bytes(b"old")
    writer = WindowsLocalDownloadPublication(destination, condition=Replace())
    writer.try_write(memoryview(b"new"))
    target = writer._target
    deadline = MutableDeadline()
    if phase == "truncate":
        original = writer._api.truncate

        def after_truncate(handle: int) -> None:
            original(handle)
            deadline.expired = True

        monkeypatch.setattr(writer._api, "truncate", after_truncate)
    elif phase == "flush":
        original = writer._api.flush

        def after_flush(handle: int) -> None:
            original(handle)
            if handle == target:
                deadline.expired = True

        monkeypatch.setattr(writer._api, "flush", after_flush)
    elif phase == "metadata":
        original_security_descriptor = writer._api.security_descriptor

        def after_metadata(handle: int) -> bytes:
            result = original_security_descriptor(handle)
            if writer.possible_local_change:
                deadline.expired = True
            return result

        monkeypatch.setattr(writer._api, "security_descriptor", after_metadata)
    else:
        original = writer._api.close

        def after_close(handle: int) -> None:
            original(handle)
            if handle == target:
                deadline.expired = True

        monkeypatch.setattr(writer._api, "close", after_close)

    try:
        with pytest.raises(LocalDownloadPartialMutationError) as stopped:
            writer.commit(
                verified_complete=True,
                size=3,
                sha256=hashlib.sha256(b"new").hexdigest(),
                deadline=cast("Deadline", deadline),
            )
        assert isinstance(stopped.value.__cause__, TimeoutError)
        assert writer.possible_local_change
        assert writer.publication_uncertain
        assert not writer.published
    finally:
        writer.abort()
    assert destination.read_bytes() == b"new"


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
