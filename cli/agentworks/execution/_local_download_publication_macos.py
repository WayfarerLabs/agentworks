"""Private macOS workstation publication of a completely verified download.

Create links a verified private stage into an absent name. Replace copies that
stage into a held ordinary file, preserving its inode and access metadata. A
failed write or close after replacement begins can leave partial new content;
``publication_uncertain`` retains that fact.
This primitive does not establish remote transfer or cleanup completeness; its
caller must do so before passing ``verified_complete=True``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution._local_download_publication_posix import _PosixLocalDownloadStage
from agentworks.execution._local_download_stage import LocalDownloadUnsupportedError
from agentworks.execution.files import Create, Replace

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution.carrier import Deadline

_CREATE = Create()
_ACL_TYPE_EXTENDED = 0x100  # Darwin sys/acl.h.


@dataclass(frozen=True)
class _Metadata:
    identity: tuple[int, int, int, int, int]
    mode: int
    uid: int
    gid: int


def _bsd_flags(observed: os.stat_result) -> int:
    flags = getattr(observed, "st_flags", None)
    if flags is None:
        raise LocalDownloadUnsupportedError("Cannot inspect local BSD flags")
    return int(flags)


def _extended_acl_tags(fd: int) -> tuple[int, ...]:
    """Enumerate bounded Darwin extended ACL tags through a held descriptor."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        get_acl = libc.acl_get_fd_np
        free_acl = libc.acl_free
        get_entry = libc.acl_get_entry
        get_tag = libc.acl_get_tag_type
        valid_acl = libc.acl_valid
    except AttributeError as exc:
        raise LocalDownloadUnsupportedError("Local extended ACL inspection is unavailable") from exc
    get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
    get_acl.restype = ctypes.c_void_p
    free_acl.argtypes = (ctypes.c_void_p,)
    free_acl.restype = ctypes.c_int
    get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    get_entry.restype = ctypes.c_int
    get_tag.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    get_tag.restype = ctypes.c_int
    valid_acl.argtypes = (ctypes.c_void_p,)
    valid_acl.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return ()
        raise LocalDownloadUnsupportedError(f"Cannot inspect local extended ACL (errno {error})")
    try:
        if valid_acl(acl) != 0:
            raise LocalDownloadUnsupportedError("Local extended ACL is malformed")
        tags: list[int] = []
        for index in range(129):
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = get_entry(acl, index, ctypes.byref(entry))
            if result == -1 and ctypes.get_errno() == errno.EINVAL:
                return tuple(tags)
            if result != 0 or not entry.value or index == 128:
                raise LocalDownloadUnsupportedError("Cannot enumerate local extended ACL")
            tag = ctypes.c_int()
            if get_tag(entry, ctypes.byref(tag)) != 0:
                raise LocalDownloadUnsupportedError("Cannot inspect local extended ACL tag")
            tags.append(tag.value)
        raise LocalDownloadUnsupportedError("Local extended ACL exceeds the supported entry bound")
    finally:
        if free_acl(acl) != 0:
            raise LocalDownloadUnsupportedError("Cannot release local extended ACL")


def _has_extended_acl(fd: int) -> bool:
    return bool(_extended_acl_tags(fd))


def _has_non_deny_acl(fd: int) -> bool:
    # A deny-only ACL cannot grant an alternate writer stage access.
    return any(tag != 2 for tag in _extended_acl_tags(fd))


def _has_xattrs(fd: int) -> bool:
    """Ask Darwin whether any accessible extended attribute names exist."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        list_names = libc.flistxattr
    except AttributeError as exc:
        raise LocalDownloadUnsupportedError("Local extended attribute inspection is unavailable") from exc
    list_names.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    list_names.restype = ctypes.c_ssize_t
    ctypes.set_errno(0)
    count = int(list_names(fd, None, 0, 0))
    if count < 0:
        raise LocalDownloadUnsupportedError(f"Cannot inspect local extended attributes (errno {ctypes.get_errno()})")
    return count != 0


def _metadata(fd: int, *, private_stage: bool = False) -> _Metadata:
    observed = os.fstat(fd)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise LocalDownloadUnsupportedError("Local destination must be an ordinary single-link regular file")
    if observed.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise LocalDownloadUnsupportedError("Local destination has privilege-bearing mode bits")
    if _bsd_flags(observed) != 0:
        raise LocalDownloadUnsupportedError("Local destination has unsupported BSD flags")
    try:
        if _has_xattrs(fd):
            raise LocalDownloadUnsupportedError("Local destination has unsupported extended attributes")
        unsupported_acl = _has_non_deny_acl(fd) if private_stage else _has_extended_acl(fd)
        if unsupported_acl:
            raise LocalDownloadUnsupportedError("Local destination has an unsupported extended ACL")
    except LocalDownloadUnsupportedError:
        raise
    except OSError as exc:
        raise LocalDownloadUnsupportedError("Cannot inspect local access metadata") from exc
    return _Metadata(
        (observed.st_dev, observed.st_ino, observed.st_ctime_ns, observed.st_mtime_ns, observed.st_size),
        stat.S_IMODE(observed.st_mode),
        observed.st_uid,
        observed.st_gid,
    )


class MacOSLocalDownloadPublication(_PosixLocalDownloadStage):
    """Own a same-directory stage and publish under ordinary caller authority.

    This class is private until its macOS filesystem and ACL behavior has native
    acceptance evidence. The caller serializes its own operations; an external
    same-user writer can race the final name check.
    """

    def __init__(self, destination: Path, *, condition: Create | Replace = _CREATE) -> None:
        if sys.platform != "darwin":
            raise LocalDownloadUnsupportedError("Local macOS publication requires macOS")
        if type(condition) not in (Create, Replace):
            raise ValueError("Local download publication requires Create or Replace")
        if (
            not {os.open, os.stat, os.link, os.unlink} <= os.supports_dir_fd
            or not {
                os.stat,
                os.link,
            }
            <= os.supports_follow_symlinks
        ):
            raise LocalDownloadUnsupportedError("Local descriptor-relative publication is unavailable")
        super().__init__(destination)
        self._target_fd: int | None = None
        self._original: _Metadata | None = None
        self._target_close_uncertain: bool = False
        try:
            self._open_parent(unsupported_acl=_has_non_deny_acl)
            if isinstance(condition, Replace):
                self._hold_target()
            else:
                self._admit_create()
            self._allocate_stage(readable=True, inspect=self._check_private_stage)
        except BaseException as setup_error:
            self._abort_failed_construction(setup_error)
            raise

    def _check_private_stage(self) -> None:
        assert self._stage_fd is not None
        metadata = _metadata(self._stage_fd, private_stage=True)
        if (metadata.uid, metadata.mode) != (os.geteuid(), 0o600):
            raise LocalDownloadUnsupportedError("Local download stage is not private to the caller")

    def _hold_target(self) -> None:
        assert self._parent_fd is not None
        named = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or named.st_nlink != 1:
            raise LocalDownloadUnsupportedError("Local destination must be an ordinary single-link regular file")
        self._target_fd = os.open(self._name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._parent_fd)
        original = _metadata(self._target_fd)
        if original.identity[:2] != (named.st_dev, named.st_ino):
            raise FileExistsError("Local destination changed while establishing write access")
        self._original = original

    @property
    def cleanup_uncertain(self) -> bool:
        return super().cleanup_uncertain or self._target_close_uncertain

    def _check_target(self) -> None:
        assert self._target_fd is not None and self._parent_fd is not None and self._original is not None
        current = _metadata(self._target_fd)
        named = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if current != self._original or current.identity[:2] != (named.st_dev, named.st_ino):
            raise FileExistsError("Local download destination changed before replacement")

    def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
        self._check_ready(verified_complete, size, sha256, deadline)
        self._check_stage_and_parent(size, inspect=self._check_private_stage)
        stage_fd = self._stage_fd
        parent_fd = self._parent_fd
        assert stage_fd is not None and parent_fd is not None
        os.fsync(stage_fd)
        if self._original is None:
            if deadline is not None and deadline.expired:
                raise TimeoutError("Local download deadline expired before publication")
            self._link_create()
        else:
            self._check_target()
            if deadline is not None and deadline.expired:
                raise TimeoutError("Local download deadline expired before publication")
            self._copy_to_held_target(size, deadline)
        self.abort()

    def _copy_to_held_target(self, size: int, deadline: Deadline | None) -> None:
        assert self._stage_fd is not None and self._target_fd is not None
        assert self._parent_fd is not None and self._original is not None
        os.lseek(self._stage_fd, 0, os.SEEK_SET)
        os.lseek(self._target_fd, 0, os.SEEK_SET)
        remaining = size
        while remaining:
            if deadline is not None and deadline.expired:
                raise TimeoutError("Local download deadline expired during replacement")
            chunk = os.read(self._stage_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("Verified local download stage became short")
            offset = 0
            while offset < len(chunk):
                if deadline is not None and deadline.expired:
                    raise TimeoutError("Local download deadline expired during replacement")
                self.publication_uncertain = True
                written = os.write(self._target_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("Local replacement write made no progress")
                offset += written
            remaining -= len(chunk)
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired during replacement")
        self.publication_uncertain = True
        os.ftruncate(self._target_fd, size)
        os.fsync(self._target_fd)
        current = _metadata(self._target_fd)
        original = self._original
        if current.identity[:2] != original.identity[:2] or (current.mode, current.uid, current.gid) != (
            original.mode,
            original.uid,
            original.gid,
        ):
            raise LocalDownloadUnsupportedError("Local replacement changed access metadata")
        named = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != current.identity[:2]:
            raise FileExistsError("Local destination name changed during replacement")
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired during replacement")
        self._close_target()
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired during replacement")
        self.published = True
        self.publication_uncertain = False

    def _close_target(self) -> None:
        if self._target_fd is not None and not self._target_close_uncertain:
            self._target_close_uncertain = True
            os.close(self._target_fd)
            self._target_fd = None
            self._target_close_uncertain = False

    def abort(self) -> None:
        """Clean only the owned stage; retain publication and close uncertainty."""
        self._close_target()
        self._abort_stage()
