"""Where each entity's lock file and each VM's holder records live.

The vm file keeps the path every release since v0.19.0 locks, so older and
newer processes contend on one file. Every file is created on first use and
never unlinked: unlinking could let two processes lock different inodes.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from ._entities import EntityKind

if TYPE_CHECKING:
    from pathlib import Path

    from ._entities import Entity


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _lock_directory(database_path: Path) -> Path:
    return database_path.parent / f".{database_path.name}.native-locks"


def _vm_directory(database_path: Path, vm: str) -> Path:
    return _lock_directory(database_path) / f"{_digest(vm)}.d"


def entity_lock_path(database_path: Path, entity: Entity) -> Path:
    """The lock file of ``entity``, creating its private directories as needed.

    ``database_path`` must already be resolved.
    """
    if entity.kind is EntityKind.VM:
        directory = _lock_directory(database_path)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory / _digest(entity.vm)
    directory = _vm_directory(database_path, entity.vm)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if entity.kind is EntityKind.GRANT_ALL:
        return directory / str(entity.kind)
    if entity.kind is EntityKind.GRANT:
        workspace, account = entity.parents
        file_key = workspace.name + "\0" + account.name
    else:
        file_key = entity.name
    return directory / f"{entity.kind}-{_digest(file_key)}"


def holders_directory(database_path: Path, vm: str) -> Path:
    """The directory of ``vm``'s holder records; it may not exist yet."""
    return _vm_directory(database_path, vm) / "holders"
