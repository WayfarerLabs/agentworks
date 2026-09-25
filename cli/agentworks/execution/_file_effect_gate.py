"""Local SQLite effect fence for one fixed Linux guest file-helper identity.

Initialization is an explicit setup action. Effect requests only open an
existing gate and hold its write transaction until their work is complete.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from ._vm_guest_identity_protocol import VMGuestIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator

_TOKEN_BYTES = 16
_BUSY_SECONDS = 5.0
_LOWER_HEX = frozenset("0123456789abcdef")


def _valid_path(path: object) -> bool:
    if type(path) is not str or not path.startswith("/") or os.path.normpath(path) != path or "\0" in path:
        return False
    try:
        return len(path.encode("utf-8")) <= 4_096
    except UnicodeEncodeError:
        return False


def _valid_scope_name(name: object) -> bool:
    if type(name) is not str or not name or "\0" in name:
        return False
    try:
        return len(name.encode("utf-8")) <= 255
    except UnicodeEncodeError:
        return False


class FileEffectGateError(ValueError):
    """The exact guest gate cannot safely authorize an effect."""


@dataclass(frozen=True, slots=True, repr=False)
class FileEffectGateBinding:
    """Durable identity and generation for one guest-local gate."""

    path: str
    instance: bytes
    generation: bytes
    guest: VMGuestIdentity
    euid: int
    scope_name: str
    proposed_generation: bytes | None = None

    def __post_init__(self) -> None:
        if (
            not _valid_path(self.path)
            or type(self.instance) is not bytes
            or len(self.instance) != _TOKEN_BYTES
            or type(self.generation) is not bytes
            or len(self.generation) != _TOKEN_BYTES
            or type(self.guest) is not VMGuestIdentity
            or type(self.euid) is not int
            or self.euid < 0
            or not _valid_scope_name(self.scope_name)
            or self.proposed_generation is not None
            and (type(self.proposed_generation) is not bytes or len(self.proposed_generation) != _TOKEN_BYTES)
        ):
            raise FileEffectGateError("invalid file-effect gate binding")
        if self.proposed_generation == self.generation:
            raise FileEffectGateError("file-effect gate generation cannot advance to itself")


def encode_file_effect_gate(binding: FileEffectGateBinding) -> dict[str, object]:
    """Encode the single durable and helper-request binding shape."""
    if type(binding) is not FileEffectGateBinding:
        raise FileEffectGateError("invalid file-effect gate binding")
    value: dict[str, object] = {
        "path": binding.path,
        "instance": binding.instance.hex(),
        "generation": binding.generation.hex(),
        "guest": {
            "instance_marker": binding.guest.instance_marker,
            "boot_id": binding.guest.boot_id,
            "init_start_ticks": binding.guest.init_start_ticks,
        },
        "euid": binding.euid,
        "scope_name": binding.scope_name,
    }
    if binding.proposed_generation is not None:
        value["proposed_generation"] = binding.proposed_generation.hex()
    return value


def decode_file_effect_gate(value: object) -> FileEffectGateBinding:
    """Decode an exact persisted or request binding at the trust boundary."""
    required = {"path", "instance", "generation", "guest", "euid", "scope_name"}
    if type(value) is not dict or not required <= set(value) <= required | {"proposed_generation"}:
        raise FileEffectGateError("invalid file-effect gate binding")
    guest = value["guest"]
    if type(guest) is not dict or set(guest) != {"instance_marker", "boot_id", "init_start_ticks"}:
        raise FileEffectGateError("invalid file-effect gate guest identity")

    def token(field: str) -> bytes:
        item = value[field]
        if (
            type(item) is not str
            or len(item) != 2 * _TOKEN_BYTES
            or any(character not in _LOWER_HEX for character in item)
        ):
            raise FileEffectGateError("invalid file-effect gate token")
        return bytes.fromhex(item)

    try:
        return FileEffectGateBinding(
            value["path"],
            token("instance"),
            token("generation"),
            VMGuestIdentity(guest["instance_marker"], guest["boot_id"], guest["init_start_ticks"]),
            value["euid"],
            value["scope_name"],
            token("proposed_generation") if "proposed_generation" in value else None,
        )
    except (KeyError, TypeError, ValueError):
        raise FileEffectGateError("invalid file-effect gate binding") from None


def _existing_gate(path: str, euid: int) -> None:
    """Reject missing, redirected, shared or unexpectedly owned gate files."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise FileEffectGateError("file-effect gate is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != euid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise FileEffectGateError("file-effect gate is unsafe")


def _connect(binding: FileEffectGateBinding) -> sqlite3.Connection:
    _existing_gate(binding.path, binding.euid)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(binding.path, safe='/')}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=_BUSY_SECONDS,
        )
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise FileEffectGateError("file-effect gate requires rollback journaling")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except (sqlite3.Error, FileEffectGateError):
        if connection is not None:
            connection.close()
        raise FileEffectGateError("file-effect gate is unavailable") from None


def _record(connection: sqlite3.Connection, binding: FileEffectGateBinding) -> bytes:
    row = connection.execute(
        "SELECT instance, generation, marker, boot_id, init_ticks, euid, scope_name FROM gate WHERE id = 1"
    ).fetchone()
    if (
        row is None
        or row[0] != binding.instance
        or row[2:]
        != (
            binding.guest.instance_marker,
            binding.guest.boot_id,
            str(binding.guest.init_start_ticks),
            binding.euid,
            binding.scope_name,
        )
    ):
        raise FileEffectGateError("file-effect gate binding changed")
    generation = row[1]
    if type(generation) is not bytes or len(generation) != _TOKEN_BYTES:
        raise FileEffectGateError("file-effect gate generation is invalid")
    return generation


def initialize_file_effect_gate(path: str, guest: VMGuestIdentity, euid: int, scope_name: str) -> FileEffectGateBinding:
    """Create a new gate during explicit setup, never during an effect request."""
    binding = FileEffectGateBinding(
        path, secrets.token_bytes(_TOKEN_BYTES), secrets.token_bytes(_TOKEN_BYTES), guest, euid, scope_name
    )
    if os.geteuid() != euid:
        raise FileEffectGateError("file-effect gate setup identity mismatch")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        with closing(_connect(binding)) as connection:
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
            connection.commit()
    except (OSError, sqlite3.Error, FileEffectGateError):
        # An incomplete file is deliberately retained: replacing its path
        # within an unresolved epoch would create another lock domain.
        raise FileEffectGateError("file-effect gate setup failed") from None
    return binding


def advance_file_effect_gate(
    binding: FileEffectGateBinding,
    current_guest: VMGuestIdentity,
) -> FileEffectGateBinding:
    """CAS an already persisted proposed generation, reconciling lost replies."""
    proposed = binding.proposed_generation
    if proposed is None or current_guest != binding.guest or os.geteuid() != binding.euid:
        raise FileEffectGateError("file-effect gate advance identity or proposal is invalid")
    connection = _connect(binding)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _record(connection, binding)
        if current == binding.generation:
            connection.execute("UPDATE gate SET generation = ? WHERE id = 1", (proposed,))
        elif current != proposed:
            raise FileEffectGateError("file-effect gate generation changed")
        connection.commit()
        return FileEffectGateBinding(
            binding.path, binding.instance, proposed, binding.guest, binding.euid, binding.scope_name
        )
    except (sqlite3.Error, FileEffectGateError):
        connection.rollback()
        raise FileEffectGateError("file-effect gate advance is uncertain") from None
    finally:
        connection.close()


@contextmanager
def hold_file_effect_gate(
    binding: FileEffectGateBinding,
    current_guest: VMGuestIdentity,
) -> Iterator[None]:
    """Hold the exact rollback write transaction around a fixed helper effect."""
    if binding.proposed_generation is not None or current_guest != binding.guest or os.geteuid() != binding.euid:
        raise FileEffectGateError("file-effect gate identity or generation is invalid")
    connection = _connect(binding)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _record(connection, binding) != binding.generation:
            raise FileEffectGateError("file-effect gate generation changed")
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
