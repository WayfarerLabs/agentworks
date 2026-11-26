"""Lock entities, their tree, the two modes, and the requests built from them."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from agentworks.errors import StateConflictError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class LockMode(IntEnum):
    """How an entity is held. A stronger mode covers a weaker one."""

    SHARED = 1
    EXCLUSIVE = 2


def modes_conflict(first: LockMode, second: LockMode) -> bool:
    """Two holds of one entity conflict unless both are shared."""
    return first is LockMode.EXCLUSIVE or second is LockMode.EXCLUSIVE


class EntityKind(StrEnum):
    """What an entity names. Declaration order is the global lock order."""

    VM = "vm"
    WORKSPACE = "workspace"
    ACCOUNT = "account"
    GRANT_ALL = "grant-all"
    PATH = "path"
    GRANT = "grant"
    SESSION = "session"
    CONSOLE = "console"


_KIND_ORDER = {kind: index for index, kind in enumerate(EntityKind)}


@dataclass(frozen=True)
class Entity:
    """One lockable thing on one VM, identified by ``(vm, kind, name)``.

    ``parents`` are not part of identity; they give the ancestors a request
    holds shared. Build entities through the constructors below, which this
    class's checks keep honest: every entity's parents are the ones its kind
    requires, all on the same VM.
    """

    vm: str
    kind: EntityKind
    name: str
    parents: tuple[Entity, ...] = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if not _fits_tree(self):
            raise ValueError(f"{self.key} on VM '{self.vm}' does not have the name and parents its kind requires")

    @property
    def key(self) -> str:
        """The display key, such as ``session/build`` or ``grant-all``, unique within one VM."""
        if self.kind is EntityKind.GRANT_ALL:
            return str(self.kind)
        return f"{self.kind}/{self.name}"

    def ancestors(self) -> Iterable[Entity]:
        """Every entity above this one, each reached once per path to it."""
        for parent in self.parents:
            yield parent
            yield from parent.ancestors()


def order_key(entity: Entity) -> tuple[int, str]:
    """The global lock order: kind order, then name. Every parent sorts before its children."""
    return (_KIND_ORDER[entity.kind], entity.name)


def vm_entity(vm: str) -> Entity:
    """The VM itself: power, provider state, trust records, VM-wide initialization."""
    return Entity(vm, EntityKind.VM, vm, ())


def workspace_entity(vm: str, name: str) -> Entity:
    """A workspace directory and its group."""
    return Entity(vm, EntityKind.WORKSPACE, name, (vm_entity(vm),))


def account_entity(vm: str, linux_user: str) -> Entity:
    """An account on the VM, an agent's or the admin's, keyed by its Linux user."""
    return Entity(vm, EntityKind.ACCOUNT, linux_user, (vm_entity(vm),))


def grant_all_entity(vm: str) -> Entity:
    """The VM's set of agents granted every workspace."""
    return Entity(vm, EntityKind.GRANT_ALL, "", (vm_entity(vm),))


def path_entity(vm: str, resolved_path: str) -> Entity:
    """A directory tree on the VM by the physical path the guest's lookup walk reported.

    Its parent is the entity of its parent directory, or the vm for a
    top-level directory; ``/`` itself is never an entity.
    """
    if not _is_path_name(resolved_path):
        raise ValueError(f"path entity {resolved_path!r} is not a normalized absolute path below '/'")
    entity = vm_entity(vm)
    prefix = ""
    for part in resolved_path[1:].split("/"):
        prefix = f"{prefix}/{part}"
        entity = Entity(vm, EntityKind.PATH, prefix, (entity,))
    return entity


def grant_entity(workspace: Entity, account: Entity) -> Entity:
    """One account's membership in one workspace's group."""
    return Entity(workspace.vm, EntityKind.GRANT, f"{workspace.name}+{account.name}", (workspace, account))


def session_entity(name: str, *, workspace: Entity, account: Entity) -> Entity:
    """One session, below its workspace and its account."""
    return Entity(workspace.vm, EntityKind.SESSION, name, (workspace, account))


def console_entity(vm: str, name: str, *, members: Iterable[Entity]) -> Entity:
    """A named console, below the vm and every session it holds."""
    return Entity(vm, EntityKind.CONSOLE, name, (vm_entity(vm), *members))


def _is_path_name(name: str) -> bool:
    return name.startswith("/") and name != "/" and all(part not in ("", ".", "..") for part in name[1:].split("/"))


def _fits_tree(entity: Entity) -> bool:
    """Whether ``entity``'s name and parents are the ones its kind requires."""
    parents = entity.parents
    if any(parent.vm != entity.vm for parent in parents):
        return False
    kinds = tuple(parent.kind for parent in parents)
    match entity.kind:
        case EntityKind.VM:
            return not parents and entity.name == entity.vm
        case EntityKind.GRANT_ALL:
            return kinds == (EntityKind.VM,) and entity.name == ""
        case EntityKind.WORKSPACE | EntityKind.ACCOUNT:
            return kinds == (EntityKind.VM,) and bool(entity.name)
        case EntityKind.PATH:
            if not _is_path_name(entity.name) or len(parents) != 1:
                return False
            directory = posixpath.dirname(entity.name)
            if directory == "/":
                return kinds == (EntityKind.VM,)
            return kinds == (EntityKind.PATH,) and parents[0].name == directory
        case EntityKind.GRANT:
            return kinds == (EntityKind.WORKSPACE, EntityKind.ACCOUNT) and (
                entity.name == f"{parents[0].name}+{parents[1].name}"
            )
        case EntityKind.SESSION:
            return kinds == (EntityKind.WORKSPACE, EntityKind.ACCOUNT) and bool(entity.name)
        case EntityKind.CONSOLE:
            return bool(entity.name) and kinds[:1] == (EntityKind.VM,) and set(kinds[1:]) <= {EntityKind.SESSION}


@dataclass(frozen=True)
class LockRequest:
    """The whole lock set one acquisition takes, on one VM.

    Every entity in ``exclusive`` is held exclusive; every other entity in
    ``shared``, and every ancestor of either, is held shared. The fields are
    tuples rather than sets because entity equality ignores parents: two
    entities with one key but different parents are two reads that disagree,
    which the request refuses before it builds its keyed map.
    """

    exclusive: tuple[Entity, ...] = ()
    shared: tuple[Entity, ...] = ()
    _modes: Mapping[Entity, LockMode] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        given = (*self.exclusive, *self.shared)
        if not given:
            raise ValueError("a lock request names no entity")
        if len({entity.vm for entity in given}) != 1:
            raise ValueError("a lock request names entities on more than one VM; hold one set per VM")
        reached: dict[Entity, frozenset[str]] = {}
        for entity in given:
            for candidate in (entity, *entity.ancestors()):
                parent_keys = frozenset(parent.key for parent in candidate.parents)
                previous = reached.setdefault(candidate, parent_keys)
                if previous != parent_keys:
                    raise StateConflictError(
                        f"{candidate.key} on VM '{candidate.vm}' was read with two different parents",
                        entity_kind=str(candidate.kind),
                        entity_name=candidate.name,
                        hint="Run the command again.",
                    )
        exclusive = set(self.exclusive)
        modes = {
            entity: LockMode.EXCLUSIVE if entity in exclusive else LockMode.SHARED
            for entity in sorted(reached, key=order_key)
        }
        object.__setattr__(self, "_modes", MappingProxyType(modes))

    @property
    def vm(self) -> str:
        """The one VM every entity of the request names."""
        return next(iter(self._modes)).vm

    def modes(self) -> Mapping[Entity, LockMode]:
        """Every entity the request holds, with its mode, in the global lock order."""
        return self._modes
