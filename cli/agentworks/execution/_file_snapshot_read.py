"""Materialize bounded regular-file content or revisions from held files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ._file_snapshot import (
    SnapshotFailureKind,
    SnapshotReadError,
    _hold_regular_file,
    _validate_inputs,
)
from ._file_stat import FileRevision, FileStat

_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileSnapshot:
    """Immutable bytes and the observations that bind them to one object."""

    data: bytes = field(repr=False)
    revision: FileRevision

    @property
    def stat(self) -> FileStat:
        return self.revision.stat

    @property
    def digest(self) -> bytes:
        digest = self.revision.digest
        assert digest is not None
        return digest


def read_snapshot(
    trusted_root_fd: int,
    relative_path: str,
    max_bytes: int,
    *,
    expires_at: float | None = None,
) -> FileSnapshot | None:
    """Read one bounded regular file relative to a borrowed directory descriptor.

    ``None`` means absence during initial traversal. Later observed changes are
    conflicts. The byte bound does not prevent a filesystem read from blocking.
    """
    components = _validate_inputs(relative_path, max_bytes, expires_at)
    result = _read_regular_file(
        trusted_root_fd,
        components,
        max_bytes=max_bytes,
        include_digest=True,
        collect_content=True,
        expires_at=expires_at,
    )
    assert result is None or isinstance(result, FileSnapshot)
    return result


def read_revision(
    trusted_root_fd: int,
    relative_path: str,
    *,
    include_digest: bool,
    expires_at: float | None = None,
) -> FileRevision | None:
    """Observe one regular file without retaining its bytes.

    A stat-only observation performs no content read. A digest observation
    hashes bounded chunks and checks the deadline between filesystem calls.
    """
    components = _validate_inputs(relative_path, 1, expires_at)
    if type(include_digest) is not bool:
        raise ValueError("Revision digest selection must be a boolean")
    result = _read_regular_file(
        trusted_root_fd,
        components,
        max_bytes=None,
        include_digest=include_digest,
        collect_content=False,
        expires_at=expires_at,
    )
    assert result is None or isinstance(result, FileRevision)
    return result


def _read_regular_file(
    trusted_root_fd: int,
    components: tuple[str, ...],
    *,
    max_bytes: int | None,
    include_digest: bool,
    collect_content: bool,
    expires_at: float | None,
) -> FileSnapshot | FileRevision | None:
    stat_only = not include_digest and not collect_content
    with _hold_regular_file(
        trusted_root_fd,
        components,
        max_bytes=max_bytes,
        stat_only=stat_only,
        expires_at=expires_at,
    ) as held:
        if held is None:
            return None

        content = bytearray() if collect_content else None
        content_hash = hashlib.sha256() if include_digest else None
        observed_size = 0
        if include_digest or collect_content:
            while True:
                request_size = _READ_CHUNK_BYTES
                if max_bytes is not None:
                    request_size = min(request_size, max_bytes - observed_size + 1)
                chunk = held.read(request_size, expires_at=expires_at)
                if not chunk:
                    break
                observed_size += len(chunk)
                if max_bytes is not None and observed_size > max_bytes:
                    raise SnapshotReadError(SnapshotFailureKind.LIMIT)
                if content is not None:
                    content.extend(chunk)
                if content_hash is not None:
                    content_hash.update(chunk)

        held.verify(expires_at=expires_at)
        if include_digest and observed_size != held.stat.size:
            raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
        revision = FileRevision(held.stat, None if content_hash is None else content_hash.digest())
        if content is None:
            return revision
        return FileSnapshot(data=bytes(content), revision=revision)
