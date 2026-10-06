"""Guest-local setup, inspection and generation advance for file effect gates."""

from __future__ import annotations

import os
import secrets
from contextlib import closing
from typing import TYPE_CHECKING

from ._file_effect_gate import (
    _TOKEN_BYTES,
    FileEffectGateBinding,
    FileEffectGateError,
    _acquire_flock,
    _check_gate_namespace,
    _check_inode,
    _connect,
    _inspect_record,
    _locked_gate,
    _open_flags,
    _record,
    _require_before_deadline,
    _valid_path,
    _valid_scope_name,
)
from ._vm_guest_identity_protocol import VMGuestIdentity

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_LINUX_UID = (1 << 32) - 1


def _check_setup_identity(path: str, guest: VMGuestIdentity, euid: int, scope_name: str) -> None:
    if (
        not _valid_path(path)
        or type(guest) is not VMGuestIdentity
        or type(euid) is not int
        or not 0 <= euid <= _MAX_LINUX_UID
        or os.geteuid() != euid
        or not _valid_scope_name(scope_name)
    ):
        raise FileEffectGateError("file-effect gate setup identity is invalid")


def _observe_setup_guest(guest: VMGuestIdentity, observe_guest: Callable[[], VMGuestIdentity]) -> None:
    observed = observe_guest()
    if type(observed) is not VMGuestIdentity or observed != guest:
        raise FileEffectGateError("file-effect gate guest identity changed")


def setup_file_effect_gate(
    path: str,
    guest: VMGuestIdentity,
    euid: int,
    scope_name: str,
    observe_guest: Callable[[], VMGuestIdentity],
    *,
    expires_at: float | None = None,
) -> FileEffectGateBinding:
    """Create or adopt the exact existing gate, never repairing an incomplete one."""
    try:
        import sqlite3
    except ImportError:
        raise FileEffectGateError("file-effect gate requires Python sqlite3") from None

    _check_setup_identity(path, guest, euid, scope_name)
    _check_gate_namespace(path, euid)
    _require_before_deadline(expires_at)
    _observe_setup_guest(guest, observe_guest)
    _require_before_deadline(expires_at)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _open_flags(), 0o600)
    except FileExistsError:
        # Another setup may still hold this inode. Inspection waits for its
        # flock, then accepts only one complete matching record.
        return inspect_file_effect_gate(path, guest, euid, scope_name, observe_guest, expires_at=expires_at)
    except OSError:
        raise FileEffectGateError("file-effect gate setup failed") from None
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        binding = FileEffectGateBinding(
            path,
            secrets.token_bytes(_TOKEN_BYTES),
            secrets.token_bytes(_TOKEN_BYTES),
            guest,
            euid,
            scope_name,
            metadata.st_dev,
            metadata.st_ino,
        )
        _acquire_flock(descriptor, expires_at)
        with closing(_connect(path, euid, descriptor, (binding.device, binding.inode))) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE gate (id INTEGER PRIMARY KEY CHECK (id = 1), instance BLOB NOT NULL, "
                "generation BLOB NOT NULL, marker TEXT NOT NULL, boot_id TEXT NOT NULL, "
                "init_ticks TEXT NOT NULL, euid INTEGER NOT NULL, scope_name TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO gate VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding.instance,
                    binding.generation,
                    guest.instance_marker,
                    guest.boot_id,
                    str(guest.init_start_ticks),
                    euid,
                    scope_name,
                ),
            )
            _require_before_deadline(expires_at)
            connection.commit()
    except (OSError, sqlite3.Error, FileEffectGateError):
        # An incomplete file is deliberately retained: replacing its path
        # within an unresolved epoch would create another lock domain.
        raise FileEffectGateError("file-effect gate setup failed") from None
    finally:
        os.close(descriptor)
    return binding


def inspect_file_effect_gate(
    path: str,
    guest: VMGuestIdentity,
    euid: int,
    scope_name: str,
    observe_guest: Callable[[], VMGuestIdentity],
    *,
    expires_at: float | None = None,
) -> FileEffectGateBinding:
    """Discover a complete existing gate after a lost setup reply, without creating or advancing it."""
    try:
        import sqlite3
    except ImportError:
        raise FileEffectGateError("file-effect gate requires Python sqlite3") from None

    _check_setup_identity(path, guest, euid, scope_name)
    _check_gate_namespace(path, euid)
    try:
        descriptor = os.open(path, os.O_RDONLY | _open_flags())
    except OSError:
        raise FileEffectGateError("file-effect gate is unavailable") from None
    try:
        metadata = _check_inode(path, euid, descriptor)
        expected = (metadata.st_dev, metadata.st_ino)
        _acquire_flock(descriptor, expires_at)
        _check_inode(path, euid, descriptor, expected)
        _observe_setup_guest(guest, observe_guest)
        _require_before_deadline(expires_at)
        with closing(_connect(path, euid, descriptor, expected)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                instance, generation = _inspect_record(connection, guest, euid, scope_name)
                _require_before_deadline(expires_at)
                connection.commit()
            except (sqlite3.Error, FileEffectGateError):
                connection.rollback()
                raise FileEffectGateError("file-effect gate inspection is uncertain") from None
        _check_inode(path, euid, descriptor, expected)
        return FileEffectGateBinding(path, instance, generation, guest, euid, scope_name, *expected)
    finally:
        os.close(descriptor)


def advance_file_effect_gate(
    binding: FileEffectGateBinding,
    observe_guest: Callable[[], VMGuestIdentity],
    *,
    expires_at: float | None = None,
) -> FileEffectGateBinding:
    """CAS an already persisted proposed generation, reconciling lost replies."""
    try:
        import sqlite3
    except ImportError:
        raise FileEffectGateError("file-effect gate requires Python sqlite3") from None

    proposed = binding.proposed_generation
    if proposed is None or os.geteuid() != binding.euid:
        raise FileEffectGateError("file-effect gate advance identity or proposal is invalid")
    with _locked_gate(binding, expires_at) as descriptor:
        if observe_guest() != binding.guest:
            raise FileEffectGateError("file-effect gate guest identity changed")
        _require_before_deadline(expires_at)
        with closing(_connect(binding.path, binding.euid, descriptor, (binding.device, binding.inode))) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = _record(connection, binding)
                if current == binding.generation:
                    _require_before_deadline(expires_at)
                    connection.execute("UPDATE gate SET generation = ? WHERE id = 1", (proposed,))
                elif current != proposed:
                    raise FileEffectGateError("file-effect gate generation changed")
                _require_before_deadline(expires_at)
                connection.commit()
            except (sqlite3.Error, FileEffectGateError):
                connection.rollback()
                raise FileEffectGateError("file-effect gate advance is uncertain") from None
        return FileEffectGateBinding(
            binding.path,
            binding.instance,
            proposed,
            binding.guest,
            binding.euid,
            binding.scope_name,
            binding.device,
            binding.inode,
        )
