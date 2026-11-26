"""Holder records: which operation holds a lock set, so a refusal can name it.

Each held set keeps one JSON record in its VM's ``holders/`` directory,
locked exclusive for as long as the set is held. A record is live exactly
while that lock is held, which the OS releases when its holder exits, so no
pid is ever probed. Records are advisory: the entity locks decide, and a
missing or stale record only turns a refusal's message generic.

Records live beside the operator's database in a 0700 directory and carry
only the command path, the entity keys and modes, the acquisition time and
the pid: never argument or option values, secrets, environment, or prompt
answers.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.deadline import Deadline

from ._entities import LockMode, modes_conflict
from ._os import try_lock, unlock

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from ._entities import Entity

operation_label: ContextVar[tuple[str, ...]] = ContextVar("native_lock_operation_label", default=("agw",))
"""The command path a holder record names, ``("agw", "workspace", "rehome")`` for example.

Only the CLI layer sets it, from the command names Click resolved, so it
never holds an argument or option value. Outside the CLI it stays ``("agw",)``.
"""

RECORD_LOCK_WAIT_SECONDS = 1.0
"""How long a holder waits for its own new record's lock, which a reader's liveness probe holds briefly."""

RECORD_LOCK_OFFSET = 1 << 62
"""The byte a record's lock takes on Windows: past the content, so readers can still read it."""

_POLL_SECONDS = 0.01
_BINARY = getattr(os, "O_BINARY", 0)
_MODE_NAMES = {LockMode.SHARED: "shared", LockMode.EXCLUSIVE: "exclusive"}
_MODES_BY_NAME = {name: mode for mode, name in _MODE_NAMES.items()}


@dataclass(frozen=True)
class HolderInfo:
    """The live holder a refusal names: its command path, the entity it holds and how, and for how long."""

    command: tuple[str, ...]
    entity: str
    mode: LockMode
    held_for: float
    """Seconds the holder had held its set when the refusal read its record."""
    pid: int
    """For diagnosis only; liveness never rests on it."""


@dataclass(frozen=True)
class _Record:
    pid: int
    command: tuple[str, ...]
    acquired: float
    entities: tuple[tuple[str, LockMode], ...]


class HolderRecord:
    """The record of one held set, locked until :meth:`retire`."""

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd

    def retire(self) -> None:
        """Unlock, close and remove the record.

        On Windows a reader may still have it open, which leaves it unlocked
        for the next reader to remove.
        """
        try:
            unlock(self._fd, offset=RECORD_LOCK_OFFSET)
        finally:
            os.close(self._fd)
            _unlink_quietly(self._path)


def write_record(directory: Path, modes: Mapping[Entity, LockMode], *, acquired: float) -> HolderRecord | None:
    """Create and lock the record of a set just acquired; ``None`` if its lock stays busy past the bound.

    A reader that probes the new file between its creation and its lock
    finds it free and removes it, so on POSIX the holder checks the file is
    still linked once locked and otherwise starts over with a new one.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "command": list(operation_label.get()),
            "acquired": acquired,
            "entities": [{"key": entity.key, "mode": _MODE_NAMES[mode]} for entity, mode in modes.items()],
        }
    ).encode()
    created = _create_locked(directory, Deadline.after(RECORD_LOCK_WAIT_SECONDS))
    if created is None:
        return None
    record = HolderRecord(*created)
    try:
        _write_all(created[1], payload)
    except BaseException:
        record.retire()
        raise
    return record


def _create_locked(directory: Path, deadline: Deadline) -> tuple[Path, int] | None:
    while True:
        path = directory / f"{secrets.token_hex(8)}.json"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | _BINARY, 0o600)
        try:
            locked = _lock_record(fd, deadline)
            if locked and (sys.platform == "win32" or os.fstat(fd).st_nlink > 0):
                return path, fd
            if locked:
                unlock(fd, offset=RECORD_LOCK_OFFSET)
        except BaseException:
            os.close(fd)
            _unlink_quietly(path)
            raise
        os.close(fd)
        if not locked:
            _unlink_quietly(path)
            return None
        # A reader removed the file before this holder locked it: start over with a new one.


def find_holder(directory: Path, refused: Entity, mode: LockMode) -> HolderInfo | None:
    """The earliest live holder whose record holds ``refused`` in a mode conflicting with ``mode``.

    Removes the stale records it meets and skips any that do not parse,
    which a reader racing its holder's first write can see.
    """
    now = time.time()
    found: tuple[float, HolderInfo] | None = None
    for path in sorted(directory.glob("*.json")):
        record = _read_live(path)
        if record is None:
            continue
        for key, held in record.entities:
            if key != refused.key or not modes_conflict(held, mode):
                continue
            if found is None or record.acquired < found[0]:
                holder = HolderInfo(record.command, key, held, max(0.0, now - record.acquired), record.pid)
                found = (record.acquired, holder)
    return None if found is None else found[1]


def _lock_record(fd: int, deadline: Deadline) -> bool:
    while not try_lock(fd, LockMode.EXCLUSIVE, offset=RECORD_LOCK_OFFSET):
        if deadline.expired:
            return False
        time.sleep(_POLL_SECONDS)
    return True


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]


def _read_live(path: Path) -> _Record | None:
    """Parse ``path`` when its holder still holds it; remove it when its holder is gone.

    A free record lock means the holder exited, or created the file and has
    not locked it yet. On POSIX the reader unlinks while holding its shared
    lock, so a holder waiting on that lock always sees the removal. On
    Windows the reader closes first; a live holder still has the file open,
    so only a dead holder's record can be deleted.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _BINARY)
    except (FileNotFoundError, PermissionError):
        # Gone already, or on Windows being deleted by its holder or another reader.
        return None
    try:
        live = not try_lock(fd, LockMode.SHARED, offset=RECORD_LOCK_OFFSET)
        if live:
            content = _read_all(fd)
        else:
            if sys.platform != "win32":
                _unlink_quietly(path)
            unlock(fd, offset=RECORD_LOCK_OFFSET)
    finally:
        os.close(fd)
    if not live:
        if sys.platform == "win32":
            _unlink_quietly(path)
        return None
    return _parse(content)


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(content: bytes) -> _Record | None:
    """Validate a record read from disk, which another process wrote and may have left half-written."""
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid, command, acquired, entities = (data.get(name) for name in ("pid", "command", "acquired", "entities"))
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(acquired, int | float):
        return None
    if not isinstance(command, list) or not all(isinstance(word, str) for word in command):
        return None
    if not isinstance(entities, list):
        return None
    held: list[tuple[str, LockMode]] = []
    for entry in entities:
        if not isinstance(entry, dict):
            return None
        key, mode_name = entry.get("key"), entry.get("mode")
        if not isinstance(key, str) or not isinstance(mode_name, str) or mode_name not in _MODES_BY_NAME:
            return None
        held.append((key, _MODES_BY_NAME[mode_name]))
    return _Record(pid, tuple(command), float(acquired), tuple(held))


def _unlink_quietly(path: Path) -> None:
    """Remove ``path`` if this process can; it may be gone already, or open in another process on Windows."""
    with contextlib.suppress(FileNotFoundError, PermissionError):
        path.unlink()
