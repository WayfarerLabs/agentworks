"""Fixed Linux guest entry point for the VM identity probe."""

from __future__ import annotations

import errno
import os
import stat
import sys
from contextlib import suppress

from ._vm_guest_identity_protocol import (
    VM_BOOT_ID_PATH,
    VM_INIT_STAT_PATH,
    VM_INSTANCE_MARKER_PATH,
    VMGuestIdentity,
    VMGuestIdentityFailure,
    VMGuestIdentityResponseError,
    encode_vm_guest_identity_failure,
    encode_vm_guest_identity_success,
)

_UNSAFE_DIRECTORY_MODE = 0o022
_MARKER_MODE = 0o444
_MARKER_BYTES = 33
_BOOT_BYTES = 37
_INIT_STAT_BYTES = 8192
_MARKER_COMPONENTS = tuple(component for component in VM_INSTANCE_MARKER_PATH.split("/") if component)
if "/" + "/".join(_MARKER_COMPONENTS) != VM_INSTANCE_MARKER_PATH or len(_MARKER_COMPONENTS) < 2:
    raise RuntimeError("VM instance marker path must be a canonical absolute path")


class _GuestRefusal(Exception):
    def __init__(self, failure: VMGuestIdentityFailure) -> None:
        self.failure = failure


def _open_flags(*, directory: bool = False, nonblocking: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    if nonblocking:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _directory_fd(parent: int, name: str, expected_owner: tuple[int, int] = (0, 0)) -> int:
    if os.open not in getattr(os, "supports_dir_fd", set()):
        raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNSAFE)
    try:
        descriptor = os.open(name, _open_flags(directory=True), dir_fd=parent)
    except FileNotFoundError:
        raise _GuestRefusal(VMGuestIdentityFailure.MARKER_MISSING) from None
    except OSError:
        raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNSAFE) from None
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNREADABLE) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_uid, metadata.st_gid) != expected_owner
        or stat.S_IMODE(metadata.st_mode) & _UNSAFE_DIRECTORY_MODE
    ):
        os.close(descriptor)
        raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNSAFE)
    return descriptor


def _open_marker(root_path: str = "/", expected_owner: tuple[int, int] = (0, 0)) -> int:
    if sys.platform != "linux":
        raise _GuestRefusal(VMGuestIdentityFailure.RUNTIME)
    root: int | None = None
    current: int | None = None
    try:
        try:
            root = os.open(root_path, _open_flags(directory=True))
        except OSError:
            raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNREADABLE) from None
        current = root
        for component in _MARKER_COMPONENTS[:-1]:
            opened = _directory_fd(current, component, expected_owner)
            if current != root:
                os.close(current)
            current = opened
        try:
            marker = os.open(_MARKER_COMPONENTS[-1], _open_flags(nonblocking=True), dir_fd=current)
        except FileNotFoundError:
            raise _GuestRefusal(VMGuestIdentityFailure.MARKER_MISSING) from None
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNSAFE) from None
            raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNREADABLE) from None
        try:
            metadata = os.fstat(marker)
        except OSError:
            os.close(marker)
            raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNREADABLE) from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_uid, metadata.st_gid) != expected_owner
            or stat.S_IMODE(metadata.st_mode) != _MARKER_MODE
            or metadata.st_nlink != 1
        ):
            os.close(marker)
            raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNSAFE)
        return marker
    finally:
        if current is not None and current != root:
            with suppress(OSError):
                os.close(current)
        if root is not None:
            with suppress(OSError):
                os.close(root)


def _read_marker(root_path: str = "/", expected_owner: tuple[int, int] = (0, 0)) -> str:
    descriptor = _open_marker(root_path, expected_owner)
    try:
        try:
            content = os.read(descriptor, _MARKER_BYTES + 1)
        except OSError:
            raise _GuestRefusal(VMGuestIdentityFailure.MARKER_UNREADABLE) from None
    finally:
        with suppress(OSError):
            os.close(descriptor)
    if len(content) != _MARKER_BYTES or not content.endswith(b"\n"):
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    try:
        marker = content[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY) from None
    return marker


def _read_boot_id(root_path: str = "/") -> str:
    path = VM_BOOT_ID_PATH if root_path == "/" else root_path.rstrip("/") + VM_BOOT_ID_PATH
    try:
        descriptor = os.open(path, _open_flags(nonblocking=True))
    except OSError:
        raise _GuestRefusal(VMGuestIdentityFailure.BOOT_ID_UNREADABLE) from None
    try:
        try:
            content = os.read(descriptor, _BOOT_BYTES + 1)
        except OSError:
            raise _GuestRefusal(VMGuestIdentityFailure.BOOT_ID_UNREADABLE) from None
    finally:
        with suppress(OSError):
            os.close(descriptor)
    if len(content) != _BOOT_BYTES or not content.endswith(b"\n"):
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    try:
        return content[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY) from None


def _read_init_start_ticks(root_path: str = "/") -> int:
    path = VM_INIT_STAT_PATH if root_path == "/" else root_path.rstrip("/") + VM_INIT_STAT_PATH
    try:
        descriptor = os.open(path, _open_flags(nonblocking=True))
    except OSError:
        raise _GuestRefusal(VMGuestIdentityFailure.INIT_START_UNREADABLE) from None
    try:
        try:
            content = os.read(descriptor, _INIT_STAT_BYTES + 1)
        except OSError:
            raise _GuestRefusal(VMGuestIdentityFailure.INIT_START_UNREADABLE) from None
    finally:
        with suppress(OSError):
            os.close(descriptor)
    if len(content) > _INIT_STAT_BYTES or not content.endswith(b"\n") or b"\n" in content[:-1]:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    # Field 22 is the process start time. The comm field may contain spaces or
    # parentheses, so fields after its final closing parenthesis are counted.
    if not content.startswith(b"1 ("):
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    closing = content.rfind(b") ")
    if closing < 3:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    fields = content[closing + 2 : -1].split()
    if len(fields) < 20 or not fields[19].isdigit() or len(fields[19]) > 20:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    start_ticks = int(fields[19])
    if start_ticks > 2**64 - 1:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY)
    return start_ticks


def _identity(root_path: str = "/", expected_owner: tuple[int, int] = (0, 0)) -> VMGuestIdentity:
    marker = _read_marker(root_path, expected_owner)
    boot_id = _read_boot_id(root_path)
    init_start_ticks = _read_init_start_ticks(root_path)
    try:
        return VMGuestIdentity(marker, boot_id, init_start_ticks)
    except ValueError:
        raise _GuestRefusal(VMGuestIdentityFailure.INVALID_IDENTITY) from None


def _write_all(data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = os.write(1, data[offset:])
        except OSError:
            return
        if written <= 0:
            return
        offset += written


def main(nonce: str) -> int:
    """Read only the fixed guest identity paths and emit one response."""
    try:
        identity = _identity()
    except _GuestRefusal as error:
        try:
            response = encode_vm_guest_identity_failure(nonce, error.failure)
        except VMGuestIdentityResponseError:
            return 2
    else:
        try:
            response = encode_vm_guest_identity_success(nonce, identity)
        except VMGuestIdentityResponseError:
            response = encode_vm_guest_identity_failure(nonce, VMGuestIdentityFailure.INVALID_IDENTITY)
    _write_all(response)
    return 0
