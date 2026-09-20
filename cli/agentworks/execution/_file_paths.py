"""Linux path opens confined beneath a caller-owned directory descriptor."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import sys
from enum import Enum

_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_RESOLVE = _RESOLVE_BENEATH | _RESOLVE_NO_SYMLINKS | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_XDEV

# Sources: arch/x86/entry/syscalls/syscall_64.tbl and include/uapi/asm-generic/unistd.h
# in https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/.
_OPENAT2_SYSCALL = {"aarch64": 437, "x86_64": 437}


class ConfinedOpenFailure(Enum):
    """Closed lookup failures that reveal no filesystem input."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    CONFLICT = "conflict"
    IO = "io"


class ConfinedOpenError(Exception):
    """A confined-open refusal carrying only its closed failure kind."""

    def __init__(self, kind: ConfinedOpenFailure) -> None:
        self.kind = kind
        super().__init__(kind.value)


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def open_linux_confined(root_fd: int, relative_path: str, flags: int) -> int | None:
    """Open one Linux path beneath a borrowed root without links or mount crossings.

    ``None`` means the path was absent. The caller owns a returned descriptor.
    There is deliberately no weaker fallback when the kernel ABI is unavailable.
    """
    if sys.platform != "linux":
        raise ConfinedOpenError(ConfinedOpenFailure.IO)
    syscall_number = _OPENAT2_SYSCALL.get(platform.machine())
    if syscall_number is None:
        raise ConfinedOpenError(ConfinedOpenFailure.IO)

    error_number: int | None = None
    try:
        path = os.fsencode(relative_path)
        how = _OpenHow(flags=flags, mode=0, resolve=_RESOLVE)
        descriptor, error_number = _linux_syscall(syscall_number, root_fd, path, how)
    except (OSError, UnicodeEncodeError, ValueError):
        descriptor = None
        error_number = None

    if descriptor is not None:
        return descriptor
    if error_number == errno.ENOENT:
        return None
    if error_number == errno.EAGAIN:
        raise ConfinedOpenError(ConfinedOpenFailure.CONFLICT)
    if error_number in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV, errno.EXDEV}:
        raise ConfinedOpenError(ConfinedOpenFailure.UNSUPPORTED_OBJECT)
    raise ConfinedOpenError(ConfinedOpenFailure.IO)


def _linux_syscall(
    syscall_number: int,
    root_fd: int,
    path: bytes,
    how: _OpenHow,
) -> tuple[int | None, int | None]:
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = int(
        syscall(
            ctypes.c_long(syscall_number),
            ctypes.c_int(root_fd),
            ctypes.c_char_p(path),
            ctypes.byref(how),
            ctypes.c_size_t(ctypes.sizeof(how)),
        )
    )
    if result >= 0:
        return result, None
    return None, ctypes.get_errno()
