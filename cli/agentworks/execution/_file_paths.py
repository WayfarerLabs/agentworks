"""Linux path opens confined beneath a caller-owned directory descriptor."""

from __future__ import annotations

import errno
import os
import platform
import sys
from contextlib import suppress
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


def normalized_root(path: str) -> bool:
    """Return whether a path is an absolute normalized POSIX root."""
    if path == "/":
        return True
    if not path.startswith("/") or "\x00" in path or path == "//" or path.endswith("/"):
        return False
    return all(component not in ("", ".", "..") for component in path.split("/")[1:])


def normalized_relative_path(path: str) -> bool:
    """Return whether a path is a nonempty normalized relative POSIX path."""
    return (
        bool(path)
        and not path.startswith("/")
        and "\x00" not in path
        and all(component not in ("", ".", "..") for component in path.split("/"))
    )


def open_linux_root(path: str) -> int | None:
    """Open a validated absolute Linux path without links or read permission.

    Mount crossings are allowed while reaching the explicitly selected root.
    ``None`` means the path was absent. The caller owns a returned descriptor.
    There is deliberately no weaker fallback when the required flags are unavailable.
    """
    if sys.platform != "linux" or not normalized_root(path):
        raise ConfinedOpenError(ConfinedOpenFailure.IO)
    required_flags = (
        getattr(os, "O_PATH", None),
        getattr(os, "O_DIRECTORY", None),
        getattr(os, "O_NOFOLLOW", None),
        getattr(os, "O_CLOEXEC", None),
    )
    if any(flag is None for flag in required_flags):
        raise ConfinedOpenError(ConfinedOpenFailure.IO)
    flags = sum(flag for flag in required_flags if flag is not None)

    descriptor: int | None = None
    return_descriptor = False
    try:
        descriptor = os.open("/", flags)
        for component in () if path == "/" else path[1:].split("/"):
            child = os.open(component, flags, dir_fd=descriptor)
            previous = descriptor
            descriptor = child
            with suppress(OSError):
                os.close(previous)
        return_descriptor = True
        return descriptor
    except (OSError, UnicodeEncodeError, ValueError) as error:
        if isinstance(error, OSError) and error.errno == errno.ENOENT:
            return None
        if isinstance(error, OSError) and error.errno == errno.EAGAIN:
            raise ConfinedOpenError(ConfinedOpenFailure.CONFLICT) from None
        if isinstance(error, OSError) and error.errno in {
            errno.ELOOP,
            errno.ENOTDIR,
            errno.ENXIO,
            errno.ENODEV,
            errno.EXDEV,
        }:
            raise ConfinedOpenError(ConfinedOpenFailure.UNSUPPORTED_OBJECT) from None
        raise ConfinedOpenError(ConfinedOpenFailure.IO) from None
    finally:
        if not return_descriptor and descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


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
        descriptor, error_number = _linux_syscall(syscall_number, root_fd, path, flags)
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
    flags: int,
) -> tuple[int | None, int | None]:
    import ctypes

    class OpenHow(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint64),
            ("mode", ctypes.c_uint64),
            ("resolve", ctypes.c_uint64),
        ]

    how = OpenHow(flags=flags, mode=0, resolve=_RESOLVE)
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
