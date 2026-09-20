"""Verified scratch streaming into Linux atomic file publication."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentworks.execution._file_publication as publication_module
import agentworks.execution._scratch as scratch_module
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    FilePublicationError,
    PublicationFailureKind,
    PublicationPhase,
    ScratchFileSource,
    publish_file,
)
from agentworks.execution._scratch import (
    ReadyScratchReference,
    begin_scratch,
    cleanup_scratch,
    verify_scratch,
    write_scratch_chunk,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file publication")


def _open_parent(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _metadata() -> CreateMetadata:
    return CreateMetadata(os.getuid(), os.getgid(), 0o600)


def _scratch_directory(path: Path) -> Path:
    directories = list(path.glob(f"{scratch_module._NAME_PREFIX}*"))
    assert len(directories) == 1
    return directories[0]


def _ready_scratch(parent_fd: int, content: bytes) -> ReadyScratchReference:
    reference = begin_scratch(parent_fd, len(content))
    digest = hashlib.sha256()
    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + scratch_module._MAX_CHUNK_BYTES]
        write_scratch_chunk(parent_fd, reference, offset, chunk, hashlib.sha256(chunk).digest())
        digest.update(chunk)
        offset += len(chunk)
    return verify_scratch(parent_fd, reference, digest.digest())


def _failure(
    parent_fd: int,
    content: ScratchFileSource,
    *,
    expires_at: float | None = None,
) -> FilePublicationError:
    with pytest.raises(FilePublicationError) as raised:
        publish_file(
            parent_fd,
            "target",
            content,
            condition=Create(),
            create_metadata=_metadata(),
            expires_at=expires_at,
        )
    return raised.value


def test_large_verified_scratch_is_copied_in_bounded_ranges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = os.urandom(5 * scratch_module._MAX_CHUNK_BYTES + 31)
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    original_write = publication_module._write
    original_read = scratch_module.read_scratch_range
    write_sizes: list[int] = []
    read_sizes: list[int] = []

    def recording_write(descriptor: int, block: memoryview) -> int:
        write_sizes.append(len(block))
        return original_write(descriptor, block)

    def recording_read(
        scratch_parent_fd: int,
        scratch_ready: ReadyScratchReference,
        offset: int,
        length: int,
        *,
        expires_at: float | None = None,
    ) -> bytes:
        read_sizes.append(length)
        return original_read(
            scratch_parent_fd,
            scratch_ready,
            offset,
            length,
            expires_at=expires_at,
        )

    monkeypatch.setattr(publication_module, "_write", recording_write)
    monkeypatch.setattr(scratch_module, "read_scratch_range", recording_read)
    try:
        revision = publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
        assert (tmp_path / "target").read_bytes() == content
        assert revision.digest == hashlib.sha256(content).digest()
        assert read_sizes and max(read_sizes) <= scratch_module._MAX_CHUNK_BYTES
        assert sum(read_sizes) == len(content)
        assert write_sizes and max(write_sizes) <= scratch_module._MAX_CHUNK_BYTES
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_scratch_short_write_loop_obeys_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"deadline-sensitive scratch"
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    original_write = publication_module._write
    moments = iter((0.0, 0.0, 0.0, 0.0, 10.0))
    writes = 0

    def short_write(descriptor: int, block: memoryview) -> int:
        nonlocal writes
        writes += 1
        return original_write(descriptor, block[:1])

    monkeypatch.setattr(publication_module, "_write", short_write)
    monkeypatch.setattr(publication_module, "time", SimpleNamespace(monotonic=lambda: next(moments)))
    monkeypatch.setattr(scratch_module, "time", SimpleNamespace(monotonic=lambda: 0.0))
    try:
        error = _failure(parent_fd, ScratchFileSource(parent_fd, ready), expires_at=5.0)
        assert error.kind is PublicationFailureKind.DEADLINE
        assert writes == 1
        assert not (tmp_path / "target").exists()
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_empty_verified_scratch_is_validated_and_published(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, b"")
    try:
        revision = publish_file(
            parent_fd,
            "target",
            ScratchFileSource(parent_fd, ready),
            condition=Create(),
            create_metadata=_metadata(),
        )
        assert (tmp_path / "target").read_bytes() == b""
        assert revision.digest == hashlib.sha256(b"").digest()
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_changed_verified_scratch_refuses_without_publication(tmp_path: Path) -> None:
    content = b"verified scratch content"
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    scratch_data = _scratch_directory(tmp_path) / scratch_module._DATA_NAME
    with scratch_data.open("r+b", buffering=0) as stream:
        stream.write(b"X")
        os.fsync(stream.fileno())
    try:
        error = _failure(parent_fd, ScratchFileSource(parent_fd, ready))
        assert error.kind is PublicationFailureKind.CONFLICT
        assert error.phase is PublicationPhase.CONTENT
        assert not (tmp_path / "target").exists()
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)


def test_scratch_iteration_deadline_remains_a_publication_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"scratch deadline"
    parent_fd = _open_parent(tmp_path)
    ready = _ready_scratch(parent_fd, content)
    expires_at = time.monotonic() + 30.0
    monkeypatch.setattr(scratch_module, "time", SimpleNamespace(monotonic=lambda: expires_at))
    try:
        error = _failure(parent_fd, ScratchFileSource(parent_fd, ready), expires_at=expires_at)
        assert error.kind is PublicationFailureKind.DEADLINE
        assert error.phase is PublicationPhase.CONTENT
        assert not (tmp_path / "target").exists()
        assert not list(tmp_path.glob(f"{publication_module._STAGE_PREFIX}*"))
        cleanup_scratch(parent_fd, ready)
    finally:
        os.close(parent_fd)
