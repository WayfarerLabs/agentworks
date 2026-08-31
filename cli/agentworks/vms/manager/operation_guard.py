"""Narrow shared/exclusive exclusion for checkpoint-sensitive VM work."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import stat
from contextvars import ContextVar
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.db import Database


class VMOperationGuardMode(StrEnum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


_LOCK_TIMEOUT_SECONDS = 0.1
_HELD_GUARDS: ContextVar[tuple[tuple[str, VMOperationGuardMode], ...]] = ContextVar(
    "agentworks_held_vm_operation_guards",
    default=(),
)


def _guard_key(db: Database, vm_name: str) -> str:
    return f"{db.path.resolve()}::{vm_name}"


def _guard_path(db: Database, vm_name: str) -> str:
    digest = hashlib.sha256(_guard_key(db, vm_name).encode()).hexdigest()[:32]
    return str(db.path.parent / f"agentworks-vm-operation-{digest}.lock")


def _ensure_private_regular_file(path: str) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        pass
    except OSError as error:
        raise StateError(f"could not create VM operation guard: {error}") from error
    else:
        os.close(descriptor)

    try:
        info = os.lstat(path)
    except OSError as error:
        raise StateError(f"could not inspect VM operation guard: {error}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StateError("VM operation guard is not a private regular file")
    # Windows' stat mode exposes only the DOS read-only bit, not the file's
    # inherited ACL. The guard lives beside the already-private state DB, so
    # the containing directory is the Windows access-control boundary.
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise StateError("VM operation guard permissions are not private")


def _held_mode(key: str) -> VMOperationGuardMode | None:
    modes = [mode for held_key, mode in _HELD_GUARDS.get() if held_key == key]
    if VMOperationGuardMode.EXCLUSIVE in modes:
        return VMOperationGuardMode.EXCLUSIVE
    if modes:
        return VMOperationGuardMode.SHARED
    return None


@contextlib.contextmanager
def vm_operation_guard(
    db: Database,
    vm_name: str,
    *,
    mode: VMOperationGuardMode,
    operation: str,
) -> Iterator[None]:
    """Hold one process-crash-releasing guard for the command span."""

    key = _guard_key(db, vm_name)
    held = _held_mode(key)
    if held is VMOperationGuardMode.EXCLUSIVE or held is mode:
        yield
        return
    if held is VMOperationGuardMode.SHARED:
        raise RuntimeError("a shared VM operation guard cannot be promoted to exclusive")

    path = _guard_path(db, vm_name)
    _ensure_private_regular_file(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=_LOCK_TIMEOUT_SECONDS)
        if mode is VMOperationGuardMode.EXCLUSIVE:
            connection.execute("BEGIN EXCLUSIVE")
        else:
            connection.execute("BEGIN")
            # A deferred transaction takes its shared lock on the first read.
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
    except sqlite3.OperationalError as error:
        if connection is not None:
            connection.close()
        if getattr(error, "sqlite_errorcode", None) not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise StateError(f"VM operation guard is unavailable: {error}") from error
        raise StateError(
            f"VM '{vm_name}' is busy with another Agentworks operation",
            entity_kind="vm",
            entity_name=vm_name,
            hint=f"Wait for the active operation to finish, then retry {operation}.",
        ) from error

    assert connection is not None
    token = _HELD_GUARDS.set((*_HELD_GUARDS.get(), (key, mode)))
    try:
        yield
    finally:
        _HELD_GUARDS.reset(token)
        try:
            connection.rollback()
        finally:
            connection.close()


def shared_vm_operation_guard(db: Database, vm_name: str, *, operation: str) -> contextlib.AbstractContextManager[None]:
    """Return the ordinary-operation side of the checkpoint guard."""

    return vm_operation_guard(
        db,
        vm_name,
        mode=VMOperationGuardMode.SHARED,
        operation=operation,
    )


def exclusive_vm_operation_guard(
    db: Database,
    vm_name: str,
    *,
    operation: str,
) -> contextlib.AbstractContextManager[None]:
    """Return the checkpoint/upgrade side of the checkpoint guard."""

    return vm_operation_guard(
        db,
        vm_name,
        mode=VMOperationGuardMode.EXCLUSIVE,
        operation=operation,
    )
