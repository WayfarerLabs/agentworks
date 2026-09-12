"""Serialize native mutations in one VM family across local processes."""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class NativeSetupBusyError(StateError):
    """Another process is mutating native state in this VM family."""


@dataclass
class NativeMutationGuard:
    """A held guard passed explicitly to nested operations in the same family."""

    database_path: Path
    vm_name: str
    _active: bool = True


@contextmanager
def native_mutation_guard(
    database_path: Path,
    vm_name: str,
    *,
    held: NativeMutationGuard | None = None,
) -> Iterator[NativeMutationGuard]:
    """Acquire without waiting; a nested operation shares its caller's held guard.

    Keep the lock file after release. Unlinking it could let competing processes
    lock different inodes. OS handle closure releases ownership after a crash.
    """
    database_path = database_path.resolve()
    if held is not None:
        if not held._active or held.database_path != database_path or held.vm_name != vm_name:
            raise StateError("native setup received a guard for a different or completed operation")
        yield held
        return

    directory = database_path.parent / f".{database_path.name}.native-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(vm_name.encode()).hexdigest()
    with (directory / name).open("a+b") as lock_file:
        # Windows byte-range locking needs a stable byte, including on an empty file.
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            _lock(lock_file.fileno())
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise NativeSetupBusyError(
                "another operation is changing native state on this VM",
                entity_kind="vm",
                entity_name=vm_name,
                hint="Retry after the other operation finishes.",
            ) from error
        guard = NativeMutationGuard(database_path, vm_name)
        try:
            yield guard
        finally:
            guard._active = False
            _unlock(lock_file.fileno())


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
