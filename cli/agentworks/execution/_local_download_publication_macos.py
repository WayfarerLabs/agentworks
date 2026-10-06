"""Private macOS workstation publication of a completely verified download.

Create links a verified private stage into an absent name. Replace copies that
stage into a held ordinary file, preserving its inode and access metadata. A
failed write or close after replacement begins can leave partial new content;
``local_mutation_started`` and ``publication_uncertain`` retain that fact.
This primitive does not establish remote transfer or cleanup completeness; its
caller must do so before passing ``verified_complete=True``.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupError,
    LocalDownloadCleanupUncertainError,
    LocalDownloadUnsupportedError,
)
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


def _has_extended_acl(fd: int) -> bool:
    """Inspect the macOS extended ACL through the descriptor, refusing unknowns."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        get_acl = libc.acl_get_fd_np
        free_acl = libc.acl_free
        get_entry = libc.acl_get_entry
    except AttributeError as exc:
        raise LocalDownloadUnsupportedError("Local extended ACL inspection is unavailable") from exc
    get_acl.argtypes = (ctypes.c_int, ctypes.c_int)
    get_acl.restype = ctypes.c_void_p
    free_acl.argtypes = (ctypes.c_void_p,)
    free_acl.restype = ctypes.c_int
    get_entry.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
    get_entry.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = get_acl(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise LocalDownloadUnsupportedError(f"Cannot inspect local extended ACL (errno {error})")
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = get_entry(acl, 0, ctypes.byref(entry))  # Inspect the first indexed entry.
        if result == 0:
            return True
        if ctypes.get_errno() == errno.EINVAL:
            return False
        raise LocalDownloadUnsupportedError("Cannot enumerate local extended ACL")
    finally:
        if free_acl(acl) != 0:
            raise LocalDownloadUnsupportedError("Cannot release local extended ACL")


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


def _metadata(fd: int) -> _Metadata:
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
        if _has_extended_acl(fd):
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


def _validate_directory_custody(observed: os.stat_result) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise LocalDownloadUnsupportedError("Local download ancestor is not a directory")
    if observed.st_uid not in (os.geteuid(), os.lstat("/").st_uid):
        raise LocalDownloadUnsupportedError("Local download directory owner is not trusted")
    if observed.st_mode & 0o022 and not observed.st_mode & stat.S_ISVTX:
        raise LocalDownloadUnsupportedError("Local download directory allows unsafe stage replacement")


class MacOSLocalDownloadPublication:
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
        self._destination: Path = destination.absolute()
        self._name: str = self._destination.name
        if not self._name or ".." in self._destination.parts:
            raise ValueError("Local download destination must name a normalized file path")
        self._parent_fd: int | None = None
        self._stage_fd: int | None = None
        self._target_fd: int | None = None
        self._stage_name: str | None = None
        self._stage_identity: tuple[int, int] | None = None
        self._original: _Metadata | None = None
        self._stage_close_uncertain: bool = False
        self._target_close_uncertain: bool = False
        self._parent_close_uncertain: bool = False
        self._ancestor_close_uncertain: bool = False
        self._digest: hashlib._Hash = hashlib.sha256()
        self._size: int = 0
        self.published: bool = False
        self.publication_uncertain: bool = False
        self.local_mutation_started: bool = False
        try:
            self._open_parent()
            if isinstance(condition, Replace):
                self._hold_target()
            else:
                try:
                    os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError("Local download destination already exists")
            for _ in range(8):
                stage_name = f".agw-download-{secrets.token_hex(16)}"
                try:
                    fd = os.open(
                        stage_name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=self._parent_fd,
                    )
                except FileExistsError:
                    continue
                self._stage_fd = fd
                self._stage_name = stage_name
                staged = os.fstat(fd)
                self._stage_identity = (staged.st_dev, staged.st_ino)
                os.fchmod(fd, 0o600)
                self._check_private_stage()
                break
            else:
                raise FileExistsError("Cannot allocate a unique local download stage")
        except BaseException as setup_error:
            try:
                self.abort()
            except BaseException as cleanup_error:
                error_type = LocalDownloadCleanupUncertainError if self.cleanup_uncertain else LocalDownloadCleanupError
                raise error_type(
                    "Local download construction left unfinished cleanup",
                    unfinished_stage=self,
                    setup_error=setup_error,
                    cleanup_error=cleanup_error,
                ) from cleanup_error
            raise

    def _open_parent(self) -> None:
        """Walk from root with held directory descriptors and inspect each ACL."""
        self._parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in ("", *self._destination.parent.parts[1:]):
            assert self._parent_fd is not None
            _validate_directory_custody(os.fstat(self._parent_fd))
            if _has_extended_acl(self._parent_fd):
                raise LocalDownloadUnsupportedError("Local download ancestor has an unsupported ACL")
            if not component:
                continue
            child_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._parent_fd)
            old_fd = self._parent_fd
            self._parent_fd = child_fd
            self._ancestor_close_uncertain = True
            os.close(old_fd)
            self._ancestor_close_uncertain = False
        assert self._parent_fd is not None
        _validate_directory_custody(os.fstat(self._parent_fd))
        if _has_extended_acl(self._parent_fd):
            raise LocalDownloadUnsupportedError("Local download ancestor has an unsupported ACL")
        bound = os.fstat(self._parent_fd)
        named = os.stat(self._destination.parent, follow_symlinks=False)
        if (bound.st_dev, bound.st_ino) != (named.st_dev, named.st_ino):
            raise FileExistsError("Local download parent changed while opening")

    def _check_private_stage(self) -> None:
        assert self._stage_fd is not None
        metadata = _metadata(self._stage_fd)
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
        return (
            self._stage_close_uncertain
            or self._target_close_uncertain
            or self._parent_close_uncertain
            or self._ancestor_close_uncertain
        )

    @property
    def possible_local_change(self) -> bool:
        return self.local_mutation_started

    def try_write(self, data: memoryview) -> int:
        if self._stage_fd is None or self.published or self.publication_uncertain or self.cleanup_uncertain:
            raise ValueError("Local download stage is closed")
        written = os.write(self._stage_fd, data)
        self._digest.update(data[:written])
        self._size += written
        return written

    def _check_stage_and_parent(self, size: int) -> None:
        assert self._stage_fd is not None and self._stage_name is not None and self._parent_fd is not None
        staged = os.fstat(self._stage_fd)
        named = os.stat(self._stage_name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (staged.st_dev, staged.st_ino) != (named.st_dev, named.st_ino) or staged.st_nlink != 1:
            raise LocalDownloadUnsupportedError("Local download stage changed before publication")
        self._check_private_stage()
        if staged.st_size != size:
            raise ValueError("Local download stage size changed")
        bound = os.fstat(self._parent_fd)
        current = os.stat(self._destination.parent, follow_symlinks=False)
        if (bound.st_dev, bound.st_ino) != (current.st_dev, current.st_ino):
            raise FileExistsError("Local download parent changed before publication")

    def _check_target(self) -> None:
        assert self._target_fd is not None and self._parent_fd is not None and self._original is not None
        current = _metadata(self._target_fd)
        named = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        if current != self._original or current.identity[:2] != (named.st_dev, named.st_ino):
            raise FileExistsError("Local download destination changed before replacement")

    def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None:
        stage_fd = self._stage_fd
        parent_fd = self._parent_fd
        stage_name = self._stage_name
        if (
            stage_fd is None
            or parent_fd is None
            or stage_name is None
            or self.published
            or self.publication_uncertain
            or self.cleanup_uncertain
        ):
            raise ValueError("Local download stage is closed")
        if not verified_complete or size != self._size or sha256 != self._digest.hexdigest():
            raise ValueError("Local download is not completely verified")
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired before publication")
        self._check_stage_and_parent(size)
        os.fsync(stage_fd)
        if self._original is None:
            if deadline is not None and deadline.expired:
                raise TimeoutError("Local download deadline expired before publication")
            self.publication_uncertain = True
            try:
                os.link(stage_name, self._name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                self.published = True
                self.publication_uncertain = False
            except BaseException:
                self._reconcile_create()
                raise
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
                self.local_mutation_started = True
                self.publication_uncertain = True
                written = os.write(self._target_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("Local replacement write made no progress")
                offset += written
            remaining -= len(chunk)
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired during replacement")
        self.local_mutation_started = True
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

    def _reconcile_create(self) -> None:
        assert self._parent_fd is not None
        try:
            destination = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
            if (destination.st_dev, destination.st_ino) == self._stage_identity:
                self.published = True
                self.publication_uncertain = False
        except BaseException:
            pass

    def _close_target(self) -> None:
        if self._target_fd is not None and not self._target_close_uncertain:
            self._target_close_uncertain = True
            os.close(self._target_fd)
            self._target_fd = None
            self._target_close_uncertain = False

    def abort(self) -> None:
        """Clean only the owned stage; retain publication and close uncertainty."""
        self._close_target()
        if self._stage_name is not None and self._stage_identity is None:
            if self._stage_fd is None or self._stage_close_uncertain:
                raise LocalDownloadUnsupportedError("Local stage identity is unavailable for cleanup")
            staged = os.fstat(self._stage_fd)
            self._stage_identity = (staged.st_dev, staged.st_ino)
        if self._stage_fd is not None and not self._stage_close_uncertain:
            self._stage_close_uncertain = True
            os.close(self._stage_fd)
            self._stage_fd = None
            self._stage_close_uncertain = False
        if self._stage_name is not None and self._parent_fd is not None and not self._parent_close_uncertain:
            with suppress(FileNotFoundError):
                staged = os.stat(self._stage_name, dir_fd=self._parent_fd, follow_symlinks=False)
                if (staged.st_dev, staged.st_ino) != self._stage_identity:
                    raise LocalDownloadUnsupportedError("Local stage cleanup name changed identity")
                os.unlink(self._stage_name, dir_fd=self._parent_fd)
            self._stage_name = None
            self._stage_identity = None
        if self._parent_fd is not None and not self._parent_close_uncertain:
            self._parent_close_uncertain = True
            os.close(self._parent_fd)
            self._parent_fd = None
            self._parent_close_uncertain = False
        if self.cleanup_uncertain:
            raise LocalDownloadCleanupUncertainError("Local download descriptor cleanup has an uncertain close outcome")
