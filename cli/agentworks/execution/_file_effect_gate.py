"""Local flock effect fence and SQLite generation for one Linux guest identity.

Gate setup is an explicit action. Effect requests only open an
existing gate and hold its inode lock until their work is complete.
"""

from __future__ import annotations

import errno
import os
import posixpath
import secrets
import stat
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from ._vm_guest_identity_protocol import VMGuestIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from sqlite3 import Connection

_TOKEN_BYTES = 16
_BUSY_SECONDS = 5.0
_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_LINUX_UID = (1 << 32) - 1
_MAX_U64 = (1 << 64) - 1


def _valid_path(path: object) -> bool:
    if type(path) is not str or not path.startswith("/") or posixpath.normpath(path) != path or "\0" in path:
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
    device: int
    inode: int
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
            or not 0 <= self.euid <= _MAX_LINUX_UID
            or not _valid_scope_name(self.scope_name)
            or type(self.device) is not int
            or not 0 <= self.device <= _MAX_U64
            or type(self.inode) is not int
            or not 0 < self.inode <= _MAX_U64
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
        "device": binding.device,
        "inode": binding.inode,
    }
    if binding.proposed_generation is not None:
        value["proposed_generation"] = binding.proposed_generation.hex()
    return value


def decode_file_effect_gate(value: object) -> FileEffectGateBinding:
    """Decode an exact persisted or request binding at the trust boundary."""
    required = {"path", "instance", "generation", "guest", "euid", "scope_name", "device", "inode"}
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
            value["device"],
            value["inode"],
            token("proposed_generation") if "proposed_generation" in value else None,
        )
    except (KeyError, TypeError, ValueError):
        raise FileEffectGateError("invalid file-effect gate binding") from None


def _open_flags() -> int:
    if sys.platform != "linux" or not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_CLOEXEC", 0):
        raise FileEffectGateError("file-effect gate requires Linux descriptor controls")
    return os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _check_inode(
    path: str,
    euid: int,
    descriptor: int,
    expected: tuple[int, int] | None = None,
) -> os.stat_result:
    """Verify the held regular inode still names the same safe gate path."""
    try:
        held = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
    except OSError:
        raise FileEffectGateError("file-effect gate is unavailable") from None
    for metadata in (held, named):
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != euid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino) != (held.st_dev, held.st_ino)
            or expected is not None
            and (metadata.st_dev, metadata.st_ino) != expected
        ):
            raise FileEffectGateError("file-effect gate inode changed or is unsafe")
    return held


def _acquire_flock(descriptor: int, expires_at: float | None) -> None:
    """Wait only to the caller's deadline, with a finite default for setup."""
    try:
        import fcntl
    except ImportError:
        raise FileEffectGateError("file-effect gate requires Linux flock") from None

    limit = time.monotonic() + _BUSY_SECONDS if expires_at is None else expires_at
    while True:
        remaining = limit - time.monotonic()
        if remaining <= 0:
            raise FileEffectGateError("file-effect gate lock deadline expired")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR}:
                raise FileEffectGateError("file-effect gate lock failed") from None
        time.sleep(min(0.01, remaining))


def _require_before_deadline(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise FileEffectGateError("file-effect gate deadline expired")


@contextmanager
def _locked_gate(binding: FileEffectGateBinding, expires_at: float | None) -> Iterator[int]:
    try:
        descriptor = os.open(binding.path, os.O_RDONLY | _open_flags())
    except OSError:
        raise FileEffectGateError("file-effect gate is unavailable") from None
    try:
        expected = (binding.device, binding.inode)
        _check_inode(binding.path, binding.euid, descriptor, expected)
        _acquire_flock(descriptor, expires_at)
        _check_inode(binding.path, binding.euid, descriptor, expected)
        yield descriptor
    finally:
        os.close(descriptor)


def _connect(path: str, euid: int, descriptor: int, expected: tuple[int, int]) -> Connection:
    """Open only while the caller owns the independent flock descriptor."""
    try:
        import sqlite3
    except ImportError:
        raise FileEffectGateError("file-effect gate requires Python sqlite3") from None

    _check_inode(path, euid, descriptor, expected)
    connection: Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{quote(path, safe='/')}?mode=rw",
            uri=True,
            isolation_level=None,
            timeout=0.0,
        )
        _check_inode(path, euid, descriptor, expected)
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise FileEffectGateError("file-effect gate requires rollback journaling")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except (sqlite3.Error, FileEffectGateError):
        if connection is not None:
            connection.close()
        raise FileEffectGateError("file-effect gate is unavailable") from None


def _record(connection: Connection, binding: FileEffectGateBinding) -> bytes:
    instance, generation = _inspect_record(connection, binding.guest, binding.euid, binding.scope_name)
    if instance != binding.instance:
        raise FileEffectGateError("file-effect gate binding changed")
    return generation


def _inspect_record(connection: Connection, guest: VMGuestIdentity, euid: int, scope_name: str) -> tuple[bytes, bytes]:
    rows = connection.execute(
        "SELECT id, instance, generation, marker, boot_id, init_ticks, euid, scope_name FROM gate LIMIT 2"
    ).fetchall()
    if len(rows) != 1:
        raise FileEffectGateError("file-effect gate record is incomplete")
    row = rows[0]
    if (
        type(row[0]) is not int
        or row[0] != 1
        or type(row[1]) is not bytes
        or len(row[1]) != _TOKEN_BYTES
        or type(row[2]) is not bytes
        or len(row[2]) != _TOKEN_BYTES
        or row[3:]
        != (
            guest.instance_marker,
            guest.boot_id,
            str(guest.init_start_ticks),
            euid,
            scope_name,
        )
    ):
        raise FileEffectGateError("file-effect gate record is incomplete or changed")
    return row[1], row[2]


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


@contextmanager
def hold_file_effect_gate(
    binding: FileEffectGateBinding,
    observe_guest: Callable[[], VMGuestIdentity],
    *,
    expires_at: float | None = None,
) -> Iterator[None]:
    """Hold the exact inode flock after closing the SQLite generation read."""
    try:
        import sqlite3
    except ImportError:
        raise FileEffectGateError("file-effect gate requires Python sqlite3") from None

    if binding.proposed_generation is not None or os.geteuid() != binding.euid:
        raise FileEffectGateError("file-effect gate identity or generation is invalid")
    with _locked_gate(binding, expires_at) as descriptor:
        if observe_guest() != binding.guest:
            raise FileEffectGateError("file-effect gate guest identity changed")
        _require_before_deadline(expires_at)
        with closing(_connect(binding.path, binding.euid, descriptor, (binding.device, binding.inode))) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if _record(connection, binding) != binding.generation:
                    raise FileEffectGateError("file-effect gate generation changed")
                _require_before_deadline(expires_at)
                connection.commit()
            except (sqlite3.Error, FileEffectGateError):
                connection.rollback()
                raise FileEffectGateError("file-effect gate observation is uncertain") from None
        _require_before_deadline(expires_at)
        yield
