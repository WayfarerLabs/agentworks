"""Create-only directory mutation, refusal and bounded failure facts."""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from agentworks.execution._file_metadata import (
    MetadataEffect,
    MetadataError,
    MetadataFailureKind,
    MetadataStep,
    create_directory,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux directory creation")


def _create(parent: int, *, uid: int | None = None, expires_at: float | None = None) -> None:
    result = create_directory(
        parent,
        "target",
        uid=os.geteuid() if uid is None else uid,
        gid=os.getegid(),
        mode=0o3770,
        expires_at=expires_at,
    )
    assert result.changed
    assert stat.S_ISDIR(result.revision.stat.mode)


@pytest.mark.parametrize("kind", ["directory", "file", "link", "dangling", "fifo"])
def test_existing_entry_refuses_without_metadata_mutation(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target"
    referent = tmp_path / "referent"
    referent.write_bytes(b"referent")
    if kind == "directory":
        target.mkdir(mode=0o700)
        (target / "child").write_bytes(b"child")
    elif kind == "file":
        target.write_bytes(b"original")
    elif kind in {"link", "dangling"}:
        target.symlink_to(referent if kind == "link" else tmp_path / "missing")
    else:
        os.mkfifo(target)
    before, referent_before = target.lstat(), referent.stat()
    parent = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        with pytest.raises(MetadataError) as raised:
            _create(parent)
    finally:
        os.close(parent)
    assert raised.value.effect is MetadataEffect.UNCHANGED
    assert target.lstat() == before
    assert referent.stat() == referent_before
    if kind == "file":
        assert target.read_bytes() == b"original"
    if kind == "directory":
        assert (target / "child").read_bytes() == b"child"


def test_create_applies_owner_and_mode(tmp_path: Path) -> None:
    parent = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        _create(parent)
    finally:
        os.close(parent)
    observed = (tmp_path / "target").stat()
    assert (observed.st_uid, observed.st_gid) == (os.geteuid(), os.getegid())
    assert stat.S_IMODE(observed.st_mode) == 0o3770


def test_racing_creator_is_refused_without_convergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_mkdir = os.mkdir

    def race(path: str, mode: int, *, dir_fd: int) -> None:
        original_mkdir(path, 0o700, dir_fd=dir_fd)
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", race)
    parent = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        with pytest.raises(MetadataError) as raised:
            _create(parent)
    finally:
        os.close(parent)
    assert raised.value.kind is MetadataFailureKind.CONFLICT
    assert raised.value.effect is MetadataEffect.UNCHANGED
    assert stat.S_IMODE((tmp_path / "target").stat().st_mode) == 0o700


@pytest.mark.parametrize("step", [MetadataStep.OWNERSHIP, MetadataStep.MODE])
@pytest.mark.parametrize("interrupted", [False, True])
def test_mutation_failure_retains_creation_and_closes_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: MetadataStep, interrupted: bool
) -> None:
    original_close = os.close
    closed: list[int] = []

    def close(descriptor: int) -> None:
        original_close(descriptor)
        closed.append(descriptor)

    def fail(*args: object) -> None:
        if interrupted:
            raise KeyboardInterrupt
        raise OSError(errno.EPERM, "fixture")

    monkeypatch.setattr(os, "close", close)
    monkeypatch.setattr(os, "chown" if step is MetadataStep.OWNERSHIP else "chmod", fail)
    parent = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        with pytest.raises(KeyboardInterrupt if interrupted else MetadataError) as raised:
            _create(parent, uid=os.geteuid() + 1 if step is MetadataStep.OWNERSHIP else None)
        error = raised.value.__cause__ if interrupted else raised.value
        assert isinstance(error, MetadataError)
        assert error.effect is MetadataEffect.UNCERTAIN
        assert error.completed_steps == (MetadataStep.CREATION,)
        assert error.attempted_step is step
        assert closed
        for descriptor in closed:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        original_close(parent)
    assert stat.S_IMODE((tmp_path / "target").stat().st_mode) == 0o700


def test_cleanup_control_retains_completed_creation_and_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_close = os.close

    def close(descriptor: int) -> None:
        original_close(descriptor)
        raise KeyboardInterrupt

    parent = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    monkeypatch.setattr(os, "close", close)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _create(parent)
    finally:
        original_close(parent)
    error = raised.value.__cause__
    assert isinstance(error, MetadataError)
    assert error.effect is MetadataEffect.PARTIAL
    assert error.completed_steps == (MetadataStep.CREATION, MetadataStep.MODE)
    assert stat.S_IMODE((tmp_path / "target").stat().st_mode) == 0o3770


def test_expired_create_does_not_mutate(tmp_path: Path) -> None:
    parent = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        with pytest.raises(MetadataError) as raised:
            _create(parent, expires_at=0.0)
    finally:
        os.close(parent)
    assert raised.value.kind is MetadataFailureKind.DEADLINE
    assert raised.value.effect is MetadataEffect.UNCHANGED
    assert not (tmp_path / "target").exists()
