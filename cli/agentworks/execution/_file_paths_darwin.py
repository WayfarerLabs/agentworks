"""Readable Darwin descriptor walks without links or descendant device crossings.

This module is packaged by ordinary file helpers. Linux VM root helpers do not
load it. Import performs no native ACL inspection or filesystem operations.
"""

from __future__ import annotations

import os
import stat
from contextlib import suppress

from ._darwin_descriptors import has_non_deny_acl
from ._file_paths import ConfinedOpenError, ConfinedOpenFailure, _open_failure, directory_path_flags


def open_darwin_root(path: str) -> int | None:
    """Walk to a validated explicit root, permitting devices on the way."""
    return _walk(None, [] if path == "/" else path[1:].split("/"), directory_path_flags())


def open_darwin_confined(root_fd: int, relative_path: str, flags: int) -> int | None:
    """Borrow one validated root and retain only a same-device descendant."""
    return _walk(root_fd, relative_path.split("/"), flags)


def _walk(root_fd: int | None, components: list[str], flags: int) -> int | None:
    owned_fd: int | None = None
    previous_fd: int | None = None
    try:
        _require_operations()
        if flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND | getattr(os, "O_PATH", 0)):
            raise ConfinedOpenError(ConfinedOpenFailure.UNSUPPORTED_OBJECT)
        device = None if root_fd is None else os.fstat(root_fd).st_dev
        if root_fd is None:
            owned_fd = root_fd = os.open("/", directory_path_flags())
        _validate_directory(root_fd)
        parent_fd = root_fd
        for index, component in enumerate(components):
            selected_flags = flags if index == len(components) - 1 else directory_path_flags()
            try:
                named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if (
                not (stat.S_ISDIR(named.st_mode) or stat.S_ISREG(named.st_mode))
                or (selected_flags & os.O_DIRECTORY and not stat.S_ISDIR(named.st_mode))
                or (device is not None and named.st_dev != device)
            ):
                raise ConfinedOpenError(ConfinedOpenFailure.UNSUPPORTED_OBJECT)
            child = os.open(
                component, selected_flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=parent_fd
            )
            previous_fd = owned_fd
            owned_fd = child
            held = os.fstat(child)
            current = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            identity = (named.st_dev, named.st_ino, stat.S_IFMT(named.st_mode))
            if any((item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode)) != identity for item in (held, current)):
                raise ConfinedOpenError(ConfinedOpenFailure.CONFLICT)
            if stat.S_ISDIR(held.st_mode):
                _validate_directory(child)
            if previous_fd is not None:
                with suppress(OSError):
                    os.close(previous_fd)
                previous_fd = None
            parent_fd = child
        result = owned_fd
        owned_fd = None
        return result
    except (OSError, UnicodeEncodeError, ValueError) as error:
        raise ConfinedOpenError(_open_failure(error.errno if isinstance(error, OSError) else None)) from None
    finally:
        for descriptor in (owned_fd, previous_fd):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)


def _require_operations() -> None:
    if (
        not {os.open, os.stat} <= os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or any(getattr(os, name, None) is None for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"))
    ):
        raise ConfinedOpenError(ConfinedOpenFailure.UNSUPPORTED_OBJECT)


def _validate_directory(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode) or has_non_deny_acl(descriptor):
        raise ConfinedOpenError(ConfinedOpenFailure.UNSUPPORTED_OBJECT)
