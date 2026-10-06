"""Linux workstation staging for verified downloads, separate from guest metadata.

The caller owns this writer and must call abort in a finally block. Commit requires
the coordinator's complete verification, byte count and digest. Publication and
cleanup errors never remove the destination. Before admitting publication, the
writer records publication_uncertain. An interrupted call confirms published only
if the destination has the exact staged inode; otherwise uncertainty remains.
Both facts survive abort. A cleanup error may follow successful publication;
abort can retry removing the remaining stage without erasing publication evidence.
Cleanup retains descriptors until close returns. An interruption after close
admission leaves cleanup_uncertain: that descriptor number is diagnostic state,
not safe recovery custody, and is never closed again because it may be reused.
Other known cleanup can proceed without erasing that uncertainty.
This includes temporary destination metadata handles. If construction fails with
unfinished cleanup, LocalDownloadCleanupError exposes the writer for retry;
LocalDownloadCleanupUncertainError also exposes close uncertainty.

Replace requires ordinary write access and preserves local mode, owner, group and
non-security xattrs (including Linux POSIX ACLs), or refuses before publication.
Set-ID files, security xattrs and unsupported inode flags explicitly refuse. New
content retains staging write timestamps. Agentworks must serialize its operations.
The last destination recheck and rename are separate syscalls: concurrent external
writers can still race replacement. This is not an inode compare-and-swap protocol.
"""

from __future__ import annotations

import array
import os
import platform
import stat
import struct
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution._local_download_publication_posix import _PosixLocalDownloadStage
from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupError as LocalDownloadCleanupError,
)
from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupUncertainError as LocalDownloadCleanupUncertainError,
)
from agentworks.execution._local_download_stage import (
    LocalDownloadUnsupportedError as LocalDownloadUnsupportedError,
)
from agentworks.execution.files import Create, Replace

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.carrier import Deadline

_CREATE = Create()
# Linux include/uapi/linux/fs.h: FS_EXTENT_FL is a filesystem layout indicator.
_EXTENT_FLAG = 0x00080000


@dataclass(frozen=True)
class _Metadata:
    identity: tuple[int, ...]
    mode: int
    uid: int
    gid: int
    flags: int
    xattrs: dict[str, bytes]


def _inode_flags(fd: int) -> int:
    """Inspect Linux flags only on hosts with the proved generic ioctl encoding."""
    if platform.machine().lower() not in ("x86_64", "aarch64") or sys.byteorder != "little":
        raise LocalDownloadUnsupportedError("Local inode flag inspection requires Linux x86_64 or aarch64")
    import fcntl

    flags = array.array("L", [0])
    # FS_IOC_GETFLAGS = _IOR('f', 1, long), Linux generic ioctl ABI.
    request = 0x80000000 | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
    try:
        fcntl.ioctl(fd, request, flags, True)
    except OSError as exc:
        raise LocalDownloadUnsupportedError("Cannot inspect local inode flags") from exc
    if flags[0] & ~_EXTENT_FLAG:
        raise LocalDownloadUnsupportedError("Local destination has unsupported inode flags")
    return flags[0]


def _metadata(fd: int) -> _Metadata:
    observed = os.fstat(fd)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise LocalDownloadUnsupportedError("Local destination must be an ordinary single-link regular file")
    if observed.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise LocalDownloadUnsupportedError("Local destination has privilege-bearing mode bits")
    flags = _inode_flags(fd)
    try:
        xattrs = {name: os.getxattr(fd, name) for name in os.listxattr(fd)}
    except OSError as exc:
        raise LocalDownloadUnsupportedError("Cannot inspect all local extended attributes") from exc
    if any(name.startswith("security.") for name in xattrs):
        raise LocalDownloadUnsupportedError("Local destination has security extended attributes")
    return _Metadata(
        (observed.st_dev, observed.st_ino, observed.st_ctime_ns, observed.st_mtime_ns, observed.st_size),
        stat.S_IMODE(observed.st_mode),
        observed.st_uid,
        observed.st_gid,
        flags,
        xattrs,
    )


class LocalDownloadPublication(_PosixLocalDownloadStage):
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
        super().__init__(destination)
        self._metadata_fd: int | None = None
        self._metadata_close_uncertain = False
        self._original: _Metadata | None = None
        try:
            self._open_parent(access_mode=os.O_PATH)
            if isinstance(condition, Replace):
                self._original = self._destination_metadata()
            else:
                self._admit_create()
            self._allocate_stage(readable=False)
        except BaseException as setup_error:
            self._abort_failed_construction(setup_error)
            raise

    def _destination_metadata(self) -> _Metadata:
        if self._metadata_fd is not None or self.cleanup_uncertain:
            raise ValueError("Local destination metadata descriptor is unavailable")
        observed = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise LocalDownloadUnsupportedError("Local destination must be an ordinary single-link regular file")
        self._metadata_fd = os.open(self._name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._parent_fd)
        fd = self._metadata_fd
        try:
            metadata = _metadata(fd)
            if metadata.identity[:2] != (observed.st_dev, observed.st_ino):
                raise FileExistsError("Local destination changed while establishing write access")
            return metadata
        finally:
            self._close_metadata_descriptor()

    def _close_metadata_descriptor(self) -> None:
        if self._metadata_fd is not None and not self._metadata_close_uncertain:
            self._metadata_close_uncertain = True
            os.close(self._metadata_fd)
            self._metadata_fd = None
            self._metadata_close_uncertain = False

    @property
    def cleanup_uncertain(self) -> bool:
        """An admitted close has no established outcome; abort cannot resolve it."""
        return super().cleanup_uncertain or self._metadata_close_uncertain

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
            preserved = _metadata(fd)
            if (preserved.mode, preserved.uid, preserved.gid, preserved.flags, preserved.xattrs) != (
                original.mode,
                original.uid,
                original.gid,
                original.flags,
                original.xattrs,
            ):
                raise LocalDownloadUnsupportedError("Local metadata did not survive staging")
        except OSError as exc:
            raise LocalDownloadUnsupportedError("Cannot preserve local destination metadata") from exc

    def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
        """Publish only after complete coordinator verification and matching bytes.

        The coordinator must also establish its deadline, ownership and remote
        cleanup obligations before passing verified_complete=True. The optional
        deadline is checked after staging I/O and just before publication. On any error,
        inspect published and publication_uncertain and call abort. Neither a
        proved publication nor an uncertain attempt can be retried by this writer.
        """
        self._check_ready(verified_complete, size, sha256, deadline)
        self._check_stage_and_parent(size)
        fd = self._stage_fd
        parent_fd = self._parent_fd
        name = self._stage_name
        assert fd is not None and parent_fd is not None and name is not None
        if self._original is not None:
            self._preserve_metadata()
        os.fsync(fd)
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired before publication")
        if self._original is not None and self._destination_metadata() != self._original:
            raise FileExistsError("Local download destination changed before replacement")
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired before publication")
        if self._original is None:
            self._link_create()
        else:
            self.publication_uncertain = True
            try:
                os.replace(name, self._name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                self.published = True
                self.publication_uncertain = False
            except BaseException:
                self._reconcile_publication(parent_fd)
                raise
        self.abort()

    def _reconcile_publication(self, parent_fd: int) -> None:
        """Retain uncertainty unless the exact staged inode proves publication."""
        try:
            destination = os.stat(self._name, dir_fd=parent_fd, follow_symlinks=False)
            if (destination.st_dev, destination.st_ino) == self._stage_identity:
                self.published = True
                self.publication_uncertain = False
        except BaseException:
            # A second interruption or failed inspection must not erase the
            # admitted attempt or replace the original publication exception.
            pass

    def abort(self) -> None:
        """Remove only the owned stage and close handles; safe after publication.

        Name cleanup errors retain safe parent custody for retry. An admitted
        close with no established outcome retains cleanup_uncertain and its
        descriptor number only as diagnostic state; it must never be reused for
        cleanup. Other known cleanup proceeds on a subsequent abort, which then
        explicitly refuses if close uncertainty remains. Publication facts are
        retained, and the destination is never removed.
        """
        self._close_metadata_descriptor()
        self._abort_stage()
