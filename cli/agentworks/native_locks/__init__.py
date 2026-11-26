"""Hierarchical native locks: one workstation operation at a time per entity of a VM.

Every native mutation locks what it changes. The rules:

- **The tree.** Locks name entities within one VM: the ``vm``; below it each
  ``workspace``, ``account`` (an agent's or the admin's, by Linux user),
  the ``grant-all`` set, and each ``path`` (a directory tree on
  the guest, whose parent is its parent directory); below a workspace and an
  account together, each ``session`` and ``grant``; and below the vm and its
  member sessions, each console. :class:`Entity` enforces each kind's parents.
- **Two modes.** An operation holds exclusive every entity it changes and
  shared every ancestor of those, which :class:`LockRequest` adds itself.
  Two holds of one entity conflict unless both are shared, so a change
  refuses and is refused by any change at, above or below it, and changes
  in non-overlapping subtrees run together. Exclusive on one parent of a
  two-parent entity does not reach it, so an operation that changes the
  descendants it finds names each of them.
- **Read, then lock the whole request.** An operation builds its request from
  what it read before locking, acquires it once, then re-reads what it
  depends on and refuses if anything changed. A set never grows; a nested
  operation calls :meth:`NativeLockSet.require` with its own request rather
  than locking again, since a second open of a held file conflicts even
  within one process.
- **One order, no waiting.** Every acquisition takes its entities in
  ``(kind, name)`` order, kinds in :class:`EntityKind` declaration order, and
  fails at once at the first refusal, releasing what it took. Nothing waits,
  so nothing deadlocks, and two contending acquisitions never both fail.
- **No upgrade.** Neither ``flock`` nor ``LockFileEx`` converts a held shared
  lock to exclusive safely, so a request names the strongest mode it needs.
- **Records are advisory.** Each held set keeps a holder record so a refusal
  can name who holds what; the OS locks alone decide, and a missing or stale
  record only makes the message generic.
- **No prompt under a set** except at the acquisitions marked
  ``prompts=True``, which show a notice before their first prompt; a prompt
  under any other held set raises (:func:`prompt_notice`).

The vm level keeps the file and byte every release since v0.19.0 locks
exclusive, so an older agw and this one exclude each other. The OS releases
every lock when its holder exits or crashes.
"""

from ._entities import (
    Entity,
    EntityKind,
    LockMode,
    LockRequest,
    account_entity,
    console_entity,
    grant_all_entity,
    grant_entity,
    path_entity,
    session_entity,
    vm_entity,
    workspace_entity,
)
from ._os import open_lock_file, try_lock, unlock
from ._records import HolderInfo, operation_label
from ._sets import NativeLockBusyError, NativeLockSet, native_locks, probe_locks, prompt_notice, require_or_acquire

__all__ = [
    "Entity",
    "EntityKind",
    "HolderInfo",
    "LockMode",
    "LockRequest",
    "NativeLockBusyError",
    "NativeLockSet",
    "account_entity",
    "console_entity",
    "grant_all_entity",
    "grant_entity",
    "native_locks",
    "open_lock_file",
    "operation_label",
    "path_entity",
    "probe_locks",
    "prompt_notice",
    "require_or_acquire",
    "session_entity",
    "try_lock",
    "unlock",
    "vm_entity",
    "workspace_entity",
]
