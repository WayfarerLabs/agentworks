"""Linux workstation staging for verified downloads, separate from guest metadata.

The caller owns this writer and must call abort in a finally block. Commit requires
the coordinator's complete verification, byte count and digest. Failed publication
never removes the destination. A cleanup error may follow successful publication;
published records that fact and abort can retry removing the remaining stage.

Replace preserves local mode, owner, group and every readable xattr (including Linux
POSIX ACLs), or refuses before publication. Agentworks must serialize its operations.
The last destination recheck and rename are separate syscalls: concurrent external
writers can still race replacement. This is not an inode compare-and-swap protocol.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution.files import Create, Replace

if TYPE_CHECKING:
    from pathlib import Path

_CREATE = Create()


class LocalDownloadUnsupportedError(OSError):
    """The host cannot establish the required local publication guarantees."""


@dataclass(frozen=True)
class _Metadata:
    identity: tuple[int, ...]
    mode: int
    uid: int
    gid: int
    atime_ns: int
    mtime_ns: int
    xattrs: dict[str, bytes]


def _metadata(fd: int) -> _Metadata:
    observed = os.fstat(fd)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise LocalDownloadUnsupportedError("Local destination must be an ordinary single-link regular file")
    try:
        xattrs = {name: os.getxattr(fd, name) for name in os.listxattr(fd)}
    except OSError as exc:
        raise LocalDownloadUnsupportedError("Cannot inspect all local extended attributes") from exc
    return _Metadata(
        (observed.st_dev, observed.st_ino, observed.st_ctime_ns, observed.st_mtime_ns, observed.st_size),
        stat.S_IMODE(observed.st_mode),
        observed.st_uid,
        observed.st_gid,
        observed.st_atime_ns,
        observed.st_mtime_ns,
        xattrs,
    )


class LocalDownloadPublication:
    """Owned same-directory stage with a ByteSink-compatible try_write method.

    Create is atomic and cannot overwrite any existing directory entry. Replace
    requires an existing ordinary single-link file both initially and immediately
    before publication. Linux only; other hosts explicitly refuse before staging.
    New files retain workstation-created 0600 permissions and local ownership.
    Local filesystem I/O is synchronous; short writes are returned to the caller.
    """

    def __init__(self, destination: Path, *, condition: Create | Replace = _CREATE) -> None:
        if sys.platform != "linux":
            raise LocalDownloadUnsupportedError("Local download publication currently requires Linux")
        if type(condition) not in (Create, Replace):
            raise ValueError("Local download publication requires Create or Replace")
        self._destination = destination.absolute()
        self._name = self._destination.name
        self._parent_fd: int | None = None
        self._stage_fd: int | None = None
        self._stage_name: str | None = None
        self._original: _Metadata | None = None
        self._digest = hashlib.sha256()
        self._size = 0
        self.published = False
        try:
            self._parent_fd = os.open(self._destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if isinstance(condition, Replace):
                self._original = self._destination_metadata()
            else:
                try:
                    os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError("Local download destination already exists")
            for _ in range(8):
                name = f".agw-download-{secrets.token_hex(16)}"
                try:
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=self._parent_fd,
                    )
                except FileExistsError:
                    continue
                self._stage_fd = fd
                self._stage_name = name
                break
            else:
                raise FileExistsError("Cannot allocate a unique local download stage")
        except BaseException:
            self.abort()
            raise

    def _destination_metadata(self) -> _Metadata:
        fd = os.open(self._name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._parent_fd)
        try:
            return _metadata(fd)
        finally:
            os.close(fd)

    def try_write(self, data: memoryview) -> int:
        """Accept one synchronous write, retaining only size and digest state."""
        if self._stage_fd is None or self.published:
            raise ValueError("Local download stage is closed")
        written = os.write(self._stage_fd, data)
        self._digest.update(data[:written])
        self._size += written
        return written

    def _preserve_metadata(self) -> None:
        original = self._original
        fd = self._stage_fd
        assert original is not None and fd is not None
        try:
            staged = os.fstat(fd)
            if (staged.st_uid, staged.st_gid) != (original.uid, original.gid):
                os.fchown(fd, original.uid, original.gid)
            os.fchmod(fd, original.mode)
            for name in os.listxattr(fd):
                if name not in original.xattrs:
                    os.removexattr(fd, name)
            for name, value in original.xattrs.items():
                os.setxattr(fd, name, value)
            os.utime(fd, ns=(original.atime_ns, original.mtime_ns))
            preserved = _metadata(fd)
            if (preserved.mode, preserved.uid, preserved.gid, preserved.xattrs) != (
                original.mode,
                original.uid,
                original.gid,
                original.xattrs,
            ):
                raise LocalDownloadUnsupportedError("Local metadata did not survive staging")
        except OSError as exc:
            raise LocalDownloadUnsupportedError("Cannot preserve local destination metadata") from exc

    def commit(self, *, verified_complete: bool, size: int, sha256: str) -> None:
        """Publish only after complete coordinator verification and matching bytes.

        The coordinator must also establish its deadline, ownership and remote
        cleanup obligations before passing verified_complete=True. On any error,
        inspect published and call abort; do not retry a published transfer.
        """
        fd = self._stage_fd
        parent_fd = self._parent_fd
        name = self._stage_name
        if fd is None or parent_fd is None or name is None or self.published:
            raise ValueError("Local download stage is closed")
        if not verified_complete or size != self._size or sha256 != self._digest.hexdigest():
            raise ValueError("Local download is not completely verified")
        staged = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (staged.st_dev, staged.st_ino) != (named.st_dev, named.st_ino) or staged.st_nlink != 1:
            raise LocalDownloadUnsupportedError("Local download stage changed before publication")
        if staged.st_size != size:
            raise ValueError("Local download stage size changed")
        bound_parent = os.fstat(parent_fd)
        current_parent = os.stat(self._destination.parent, follow_symlinks=False)
        if (bound_parent.st_dev, bound_parent.st_ino) != (current_parent.st_dev, current_parent.st_ino):
            raise FileExistsError("Local download parent changed before publication")
        if self._original is not None:
            self._preserve_metadata()
        os.fsync(fd)
        if self._original is None:
            os.link(name, self._name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        else:
            if self._destination_metadata() != self._original:
                raise FileExistsError("Local download destination changed before replacement")
            os.replace(name, self._name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            self._stage_name = None
        self.published = True
        self.abort()

    def abort(self) -> None:
        """Remove only the owned stage and close handles; safe after publication.

        Cleanup errors propagate, retaining the parent handle and stage name for
        a retry. The destination is never removed, even after commit fails.
        """
        if self._stage_fd is not None:
            fd, self._stage_fd = self._stage_fd, None
            os.close(fd)
        if self._stage_name is not None and self._parent_fd is not None:
            with suppress(FileNotFoundError):
                os.unlink(self._stage_name, dir_fd=self._parent_fd)
            self._stage_name = None
        if self._parent_fd is not None:
            fd, self._parent_fd = self._parent_fd, None
            os.close(fd)
