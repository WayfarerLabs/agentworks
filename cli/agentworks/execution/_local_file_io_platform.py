"""Held POSIX objects and reused Win32 read/identity primitives for local I/O."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.execution._local_download_stage import LocalDownloadUnsupportedError
from agentworks.execution._local_file_io_types import (
    LocalFileCleanupUncertainError,
    LocalFileUnsupportedError,
    LocalIdentity,
    check_deadline,
)
from agentworks.execution.files import FileKind

if TYPE_CHECKING:
    from os import _ScandirIterator
    from pathlib import Path

    from agentworks.execution._local_download_publication_windows import _WindowsAPI
    from agentworks.execution.carrier import Deadline


@dataclass(eq=False, repr=False)
class _HeldHandle:
    token: int | None
    path: Path
    parent: _HeldHandle | None = None
    uncertain: bool = False

    @property
    def descriptor(self) -> int:
        assert self.token is not None
        return self.token


@dataclass(eq=False, repr=False)
class _HeldScan:
    original: _ScandirIterator[str] | None = None
    uncertain: bool = False

    @property
    def iterator(self) -> _ScandirIterator[str]:
        assert self.original is not None
        return self.original


def _posix_identity(observed: os.stat_result) -> LocalIdentity:
    if stat.S_ISDIR(observed.st_mode):
        kind = FileKind.DIRECTORY
    elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
        kind = FileKind.REGULAR
    else:
        raise LocalFileUnsupportedError("Local source must be an ordinary directory or single-link file")
    return LocalIdentity(
        "posix",
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        (observed.st_mtime_ns, observed.st_ctime_ns),
        observed.st_mode,
        observed.st_nlink,
        kind,
    )


class PosixHandles:
    """Retain actual original handles, including interrupted scan/close custody."""

    def __init__(self) -> None:
        self.handles: list[_HeldHandle] = []
        self.scans: list[_HeldScan] = []

    @property
    def settled(self) -> bool:
        return not self.handles and not self.scans

    @staticmethod
    def _observation_flags() -> int:
        return os.O_PATH if sys.platform == "linux" else os.O_EVTONLY  # type: ignore[attr-defined]

    def open_path(self, path: Path, *, directory: bool | None, deadline: Deadline) -> _HeldHandle:
        check_deadline(deadline)
        flags = os.O_RDONLY if path == path.parent and directory else self._observation_flags()
        root = _HeldHandle(None, path.__class__("/"))
        self.handles.append(root)
        root.token = os.open("/", flags | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = root
        components = path.parts[1:]
        for index, component in enumerate(components):
            current = self.child(
                current,
                component,
                directory=None if index < len(components) - 1 else directory,
                deadline=deadline,
            )
            if index < len(components) - 1 and self.observe(current).kind is not FileKind.DIRECTORY:
                raise LocalFileUnsupportedError("Local ancestor is not an ordinary directory")
        return current

    def child(self, parent: _HeldHandle, name: str, *, directory: bool | None, deadline: Deadline) -> _HeldHandle:
        check_deadline(deadline)
        flags = (self._observation_flags() if directory is None else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        held = _HeldHandle(None, parent.path / name, parent)
        self.handles.append(held)
        held.token = os.open(name, flags, dir_fd=parent.descriptor)
        observed = self.observe(held)
        if directory and observed.kind is not FileKind.DIRECTORY:
            raise LocalFileUnsupportedError("Local ancestor is not an ordinary directory")
        return held

    def observe(self, held: _HeldHandle) -> LocalIdentity:
        return _posix_identity(os.fstat(held.descriptor))

    def observe_named(self, held: _HeldHandle) -> LocalIdentity:
        if held.parent is None:
            return _posix_identity(os.stat(held.path, follow_symlinks=False))
        return _posix_identity(os.stat(held.path.name, dir_fd=held.parent.descriptor, follow_symlinks=False))

    def read(self, held: _HeldHandle, maximum: int) -> bytes:
        return os.read(held.descriptor, maximum)

    def scan(self, held: _HeldHandle) -> _HeldScan:
        scan = _HeldScan()
        self.scans.append(scan)
        scan.original = os.scandir(held.descriptor)
        return scan

    def close_scan(self, scan: _HeldScan) -> None:
        if scan.original is not None:
            scan.uncertain = True
            scan.iterator.close()
        self.scans.remove(scan)

    def mkdir(self, parent: _HeldHandle, name: str) -> None:
        os.mkdir(name, 0o700, dir_fd=parent.descriptor)

    def stat_child(self, parent: _HeldHandle, name: str, deadline: Deadline) -> LocalIdentity:
        check_deadline(deadline)
        return _posix_identity(os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False))

    def restrict_directory(self, held: _HeldHandle) -> None:
        os.fchmod(held.descriptor, 0o700)

    def close_handle(self, held: _HeldHandle) -> None:
        if held.token is not None:
            held.uncertain = True
            os.close(held.descriptor)
        self.handles.remove(held)

    def close(self) -> None:
        first: BaseException | None = None
        for scan in tuple(reversed(self.scans)):
            if not scan.uncertain:
                try:
                    self.close_scan(scan)
                except BaseException as exc:
                    if first is None:
                        first = exc
        for held in tuple(reversed(self.handles)):
            if not held.uncertain:
                try:
                    self.close_handle(held)
                except BaseException as exc:
                    if first is None:
                        first = exc
        if first is not None:
            raise first
        if not self.settled:
            raise LocalFileCleanupUncertainError("Local original resource close is uncertain")


class WindowsHandles(PosixHandles):
    """Read-only disk admission, with no delete-shared ancestor handles."""

    def __init__(self) -> None:
        super().__init__()
        self._api: _WindowsAPI | None = None
        self._pending = b""

    def _windows_api(self) -> _WindowsAPI:
        if self._api is None:
            from agentworks.execution._local_download_publication_windows import _WindowsAPI

            self._api = _WindowsAPI()
        return self._api

    def open_path(self, path: Path, *, directory: bool | None, deadline: Deadline) -> _HeldHandle:
        current: _HeldHandle | None = None
        for ancestor in reversed((path, *path.parents)):
            current = self._open(
                ancestor,
                directory=directory if ancestor == path else True,
                deadline=deadline,
                parent=current,
            )
        assert current is not None
        return current

    def _open(
        self,
        path: Path,
        *,
        directory: bool | None,
        deadline: Deadline,
        parent: _HeldHandle | None = None,
    ) -> _HeldHandle:
        check_deadline(deadline)
        # Directory listing/read attributes, or GENERIC_READ. Never WRITE/READ_CONTROL.
        access = (0x80 | 1) if directory else (0x80 if directory is None else 0x80000000)
        flags = 0x02000000 | 0x00200000
        held = _HeldHandle(None, path, parent)
        self.handles.append(held)
        held.token = self._windows_api().open(path, access, 1 | 2, 3, flags)
        observed = self.observe(held)
        if directory and observed.kind is not FileKind.DIRECTORY:
            raise LocalFileUnsupportedError("Local ancestor is not an ordinary directory")
        return held

    def child(self, parent: _HeldHandle, name: str, *, directory: bool | None, deadline: Deadline) -> _HeldHandle:
        return self._open(parent.path / name, directory=directory, deadline=deadline, parent=parent)

    def observe(self, held: _HeldHandle) -> LocalIdentity:
        try:
            observed = self._windows_api().info(held.descriptor)
        except LocalDownloadUnsupportedError as exc:
            raise LocalFileUnsupportedError("Local disk object identity is unsupported") from exc
        if observed.attributes & 0x400:
            raise LocalFileUnsupportedError("Local object is a reparse point")
        kind = FileKind.DIRECTORY if observed.attributes & 0x10 else FileKind.REGULAR
        if kind is FileKind.REGULAR and (observed.links != 1 or observed.attributes & ~0x20A6):
            raise LocalFileUnsupportedError("Local source is not an ordinary single-link file")
        return LocalIdentity(
            "windows",
            observed.identity.volume,
            observed.identity.file_id,
            observed.size,
            (*observed.created, *observed.modified),
            observed.attributes,
            observed.links,
            kind,
        )

    def observe_named(self, held: _HeldHandle) -> LocalIdentity:
        # Every component, including the leaf, is retained without FILE_SHARE_DELETE.
        return self.observe(held)

    def read(self, held: _HeldHandle, maximum: int) -> bytes:
        if not self._pending:
            self._pending = self._windows_api().read(held.descriptor)
        result, self._pending = self._pending[:maximum], self._pending[maximum:]
        return result

    def scan(self, held: _HeldHandle) -> _HeldScan:
        scan = _HeldScan()
        self.scans.append(scan)
        scan.original = os.scandir(held.path)
        return scan

    def mkdir(self, parent: _HeldHandle, name: str) -> None:
        # CreateDirectoryW is one level and applies the caller's ordinary inheritance.
        import ctypes
        from ctypes import wintypes

        api = self._windows_api()
        create = api.kernel.CreateDirectoryW
        create.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        create.restype = wintypes.BOOL
        if not create(api.path(parent.path / name), None):
            raise api._error()

    def stat_child(self, parent: _HeldHandle, name: str, deadline: Deadline) -> LocalIdentity:
        held = self._open(parent.path / name, directory=None, deadline=deadline, parent=parent)
        observed = self.observe(held)
        self.close_handle(held)
        return observed

    def restrict_directory(self, held: _HeldHandle) -> None:
        pass

    def close_handle(self, held: _HeldHandle) -> None:
        if held.token is not None:
            held.uncertain = True
            self._windows_api().close(held.descriptor)
        self.handles.remove(held)
