"""Shared held-directory and owned-stage mechanics for the two POSIX publishers."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from agentworks.execution._local_download_stage import (
    LocalDownloadCleanupError,
    LocalDownloadCleanupUncertainError,
    LocalDownloadStage,
    LocalDownloadUnsupportedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentworks.execution.carrier import Deadline


def _normalized_destination(destination: Path) -> Path:
    """Reject non-normal components before any path is opened or made absolute."""
    if not destination.name or ".." in destination.parts:
        raise ValueError("Local download destination must name a normalized file path")
    return destination.absolute()


def _validate_directory_custody(observed: os.stat_result) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise LocalDownloadUnsupportedError("Local download ancestor is not a directory")
    if observed.st_uid not in (os.geteuid(), os.lstat("/").st_uid):
        raise LocalDownloadUnsupportedError("Local download directory owner is not trusted")
    if observed.st_mode & 0o022 and not observed.st_mode & stat.S_ISVTX:
        raise LocalDownloadUnsupportedError("Local download directory allows unsafe stage replacement")


class _PosixLocalDownloadStage:
    """The common owned stage; Linux and macOS retain distinct Replace semantics."""

    def __init__(self, destination: Path) -> None:
        self._destination = _normalized_destination(destination)
        self._name = self._destination.name
        self._parent_fd: int | None = None
        self._stage_fd: int | None = None
        self._stage_name: str | None = None
        self._stage_identity: tuple[int, int] | None = None
        self._stage_close_uncertain = False
        self._parent_close_uncertain = False
        self._ancestor_close_uncertain = False
        self._digest = hashlib.sha256()
        self._size = 0
        self.published = False
        self.publication_uncertain = False

    @property
    def cleanup_uncertain(self) -> bool:
        return self._stage_close_uncertain or self._parent_close_uncertain or self._ancestor_close_uncertain

    def _open_parent(self, *, access_mode: int, unsupported_acl: Callable[[int], bool] | None = None) -> None:
        """Walk from root, holding and checking each directory before the next open."""
        flags = access_mode | os.O_DIRECTORY | os.O_NOFOLLOW
        self._parent_fd = os.open("/", flags)
        for component in ("", *self._destination.parent.parts[1:]):
            assert self._parent_fd is not None
            _validate_directory_custody(os.fstat(self._parent_fd))
            if unsupported_acl is not None and unsupported_acl(self._parent_fd):
                raise LocalDownloadUnsupportedError("Local download ancestor has an unsupported ACL")
            if not component:
                continue
            try:
                child_fd = os.open(component, flags, dir_fd=self._parent_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise LocalDownloadUnsupportedError("Local download ancestor is not a held directory") from exc
                raise
            old_fd = self._parent_fd
            self._parent_fd = child_fd
            self._ancestor_close_uncertain = True
            os.close(old_fd)
            self._ancestor_close_uncertain = False
        assert self._parent_fd is not None
        _validate_directory_custody(os.fstat(self._parent_fd))
        if unsupported_acl is not None and unsupported_acl(self._parent_fd):
            raise LocalDownloadUnsupportedError("Local download ancestor has an unsupported ACL")
        bound = os.fstat(self._parent_fd)
        named = os.stat(self._destination.parent, follow_symlinks=False)
        if (bound.st_dev, bound.st_ino) != (named.st_dev, named.st_ino):
            raise FileExistsError("Local download parent changed while opening")

    def _admit_create(self) -> None:
        try:
            os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise FileExistsError("Local download destination already exists")

    def _allocate_stage(self, *, readable: bool, inspect: Callable[[], None] | None = None) -> None:
        assert self._parent_fd is not None
        for _ in range(8):
            name = f".agw-download-{secrets.token_hex(16)}"
            try:
                fd = os.open(
                    name,
                    (os.O_RDWR if readable else os.O_WRONLY) | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self._parent_fd,
                )
            except FileExistsError:
                continue
            self._stage_fd = fd
            self._stage_name = name
            staged = os.fstat(fd)
            self._stage_identity = (staged.st_dev, staged.st_ino)
            os.fchmod(fd, 0o600)
            if inspect is not None:
                inspect()
            return
        raise FileExistsError("Cannot allocate a unique local download stage")

    def try_write(self, data: memoryview) -> int:
        """Count only bytes accepted by the stage descriptor."""
        if self._stage_fd is None or self.published or self.publication_uncertain or self.cleanup_uncertain:
            raise ValueError("Local download stage is closed")
        written = os.write(self._stage_fd, data)
        if not 0 <= written <= len(data):
            raise OSError("Local download stage returned an invalid write count")
        self._digest.update(data[:written])
        self._size += written
        return written

    def _check_ready(self, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None) -> None:
        if (
            self._stage_fd is None
            or self._parent_fd is None
            or self._stage_name is None
            or self.published
            or self.publication_uncertain
            or self.cleanup_uncertain
        ):
            raise ValueError("Local download stage is closed")
        if not verified_complete or size != self._size or sha256 != self._digest.hexdigest():
            raise ValueError("Local download is not completely verified")
        if deadline is not None and deadline.expired:
            raise TimeoutError("Local download deadline expired before publication")

    def _check_stage_and_parent(self, size: int, *, inspect: Callable[[], None] | None = None) -> None:
        assert self._stage_fd is not None and self._stage_name is not None and self._parent_fd is not None
        staged = os.fstat(self._stage_fd)
        named = os.stat(self._stage_name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (staged.st_dev, staged.st_ino) != (named.st_dev, named.st_ino) or staged.st_nlink != 1:
            raise LocalDownloadUnsupportedError("Local download stage changed before publication")
        if inspect is not None:
            inspect()
        if staged.st_size != size:
            raise ValueError("Local download stage size changed")
        bound = os.fstat(self._parent_fd)
        current = os.stat(self._destination.parent, follow_symlinks=False)
        if (bound.st_dev, bound.st_ino) != (current.st_dev, current.st_ino):
            raise FileExistsError("Local download parent changed before publication")

    def _link_create(self) -> None:
        assert self._stage_name is not None and self._parent_fd is not None
        self.publication_uncertain = True
        try:
            os.link(
                self._stage_name,
                self._name,
                src_dir_fd=self._parent_fd,
                dst_dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
            self.published = True
            self.publication_uncertain = False
        except BaseException:
            self._reconcile_create()
            raise

    def _reconcile_create(self) -> None:
        assert self._parent_fd is not None
        try:
            destination = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
            if (destination.st_dev, destination.st_ino) == self._stage_identity:
                self.published = True
                self.publication_uncertain = False
        except BaseException:
            pass

    def _abort_stage(self) -> None:
        """Clean only the exact owned name and known handles, never the destination."""
        if self._stage_name is not None and self._stage_identity is None:
            if self._stage_fd is None or self._stage_close_uncertain:
                raise LocalDownloadUnsupportedError("Local download stage identity is unavailable for cleanup")
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
                    raise LocalDownloadUnsupportedError("Local download cleanup name changed identity")
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

    def _abort_failed_construction(self, setup_error: BaseException) -> None:
        try:
            self.abort()
        except BaseException as cleanup_error:
            error_type = LocalDownloadCleanupUncertainError if self.cleanup_uncertain else LocalDownloadCleanupError
            raise error_type(
                "Local download construction left unfinished cleanup",
                unfinished_stage=cast("LocalDownloadStage", self),
                setup_error=setup_error,
                cleanup_error=cleanup_error,
            ) from cleanup_error

    def abort(self) -> None:
        raise NotImplementedError
