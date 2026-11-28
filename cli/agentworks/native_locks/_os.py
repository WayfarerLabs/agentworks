"""The platform calls: open a lock file, try a shared or exclusive lock without waiting, unlock.

POSIX (Linux and macOS) uses ``flock``, which locks the whole open file
description. Windows uses ``LockFileEx`` through ``ctypes``, because
``msvcrt.locking`` has no shared mode, on one byte at ``offset``; POSIX
ignores ``offset``. Both modes of an entity lock take byte 0, the byte every
release since v0.19.0 locks with ``msvcrt.locking``, so an older process
and a new one refuse each other in either order.

Within one process two opens of one file conflict like two processes on
both platforms, so a nested operation must reuse its caller's handle rather
than open the file again.
"""

from __future__ import annotations

import errno
import os
import sys
from typing import TYPE_CHECKING

from ._entities import LockMode

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO


def open_lock_file(path: Path) -> BinaryIO:
    """Open ``path`` as an entity lock file, creating it with its one byte when new.

    The byte gives Windows a stable range to lock, including on a file just
    created. The file is opened non-inheritable (Python's default), so no
    child process can keep its lock alive.
    """
    lock_file = path.open("a+b")
    try:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
    except BaseException:
        lock_file.close()
        raise
    return lock_file


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [  # noqa: RUF012  # ctypes reads the class attribute.
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _LPOVERLAPPED = ctypes.POINTER(_OVERLAPPED)
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        _LPOVERLAPPED,
    )
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, _LPOVERLAPPED)
    _kernel32.UnlockFileEx.restype = wintypes.BOOL

    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33

    def _overlapped(offset: int) -> _OVERLAPPED:
        overlapped = _OVERLAPPED()
        overlapped.Offset = offset & 0xFFFFFFFF
        overlapped.OffsetHigh = offset >> 32
        return overlapped

    def try_lock(fd: int, mode: LockMode, *, offset: int = 0) -> bool:
        """Lock one byte of ``fd`` at ``offset`` in ``mode`` without waiting; ``False`` when another holds it."""
        flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if mode is LockMode.EXCLUSIVE else 0)
        overlapped = _overlapped(offset)
        if _kernel32.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(overlapped)):
            return True
        code = ctypes.get_last_error()
        if code == _ERROR_LOCK_VIOLATION:
            return False
        raise ctypes.WinError(code)

    def unlock(fd: int, *, offset: int = 0) -> None:
        """Release the byte :func:`try_lock` took. Unlock before closing: Windows frees locks left at close lazily."""
        overlapped = _overlapped(offset)
        if not _kernel32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())

else:
    import fcntl

    def try_lock(fd: int, mode: LockMode, *, offset: int = 0) -> bool:  # noqa: ARG001  # Windows-only offset.
        """Lock ``fd`` in ``mode`` without waiting; ``False`` when another holds it."""
        operation = fcntl.LOCK_EX if mode is LockMode.EXCLUSIVE else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno != errno.EWOULDBLOCK:
                raise
            return False
        return True

    def unlock(fd: int, *, offset: int = 0) -> None:  # noqa: ARG001  # Windows-only offset.
        """Release the lock :func:`try_lock` took."""
        fcntl.flock(fd, fcntl.LOCK_UN)
