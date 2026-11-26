"""Acquiring, probing, reusing and releasing lock sets, and the prompt check under them."""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import StateError

from ._entities import EntityKind, LockMode, order_key, vm_entity
from ._files import entity_lock_path, holders_directory
from ._os import open_lock_file, try_lock, unlock
from ._records import find_holder, write_record

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path
    from typing import BinaryIO

    from ._entities import Entity, LockRequest
    from ._records import HolderInfo


class NativeLockBusyError(StateError):
    """A lock set was refused because another operation holds a conflicting lock.

    ``entity`` is the refused entity (also named by ``entity_kind`` and
    ``entity_name``), ``mode`` the mode this operation asked for it, and
    ``holder`` the live holder its record names, or ``None`` when no record
    names one (an older agw, or a holder that is releasing). A command that
    recorded a new intent before this refusal may set ``restore_intent``;
    this module only carries it. Code reads these fields; the message is
    for people.
    """

    def __init__(self, request: LockRequest, refused: Entity, mode: LockMode, holder: HolderInfo | None) -> None:
        exclusive = [entity for entity, held in request.modes().items() if held is LockMode.EXCLUSIVE]
        subject = exclusive[0] if exclusive else min(request.shared, key=order_key)
        if holder is None:
            reason = f"another operation holds {refused.key}"
        else:
            command = " ".join(holder.command)
            reason = f"`{command}` has held {holder.entity} for {format_duration(holder.held_for)}"
        super().__init__(
            f"{subject.key} on VM '{request.vm}' is blocked: {reason}",
            entity_kind=str(refused.kind),
            entity_name=refused.name,
            hint="Retry when that operation finishes.",
        )
        self.entity = refused
        self.mode = mode
        self.holder = holder
        self.restore_intent: tuple[str, ...] | None = None


def format_duration(seconds: float) -> str:
    """``41s``, ``3m12s`` or ``1h05m``."""
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


class NativeLockSet:
    """A held lock set, passed to nested operations so they reuse it rather than lock again.

    Its entity map is fixed at acquisition, so other threads of the same
    operation may read it; only the owning thread releases it.
    """

    def __init__(self, database_path: Path, request: LockRequest, *, prompts: bool) -> None:
        self.database_path = database_path
        self.vm = request.vm
        self.prompts = prompts
        self._modes = request.modes()
        self._active = True
        self._notice_shown = False

    @property
    def modes(self) -> Mapping[Entity, LockMode]:
        """Every entity the set holds, with its mode, in lock order."""
        return self._modes

    def covers(self, entity: Entity, mode: LockMode) -> bool:
        """Whether the set holds ``entity`` in ``mode`` or stronger, or holds the vm exclusive.

        Only the vm's exclusive covers what it does not name: an exclusive on
        any other ancestor says nothing about a second parent.
        """
        if entity.vm != self.vm:
            return False
        if self._modes.get(vm_entity(self.vm)) is LockMode.EXCLUSIVE:
            return True
        held = self._modes.get(entity)
        return held is not None and held >= mode

    def require(self, request: LockRequest, *, database_path: Path) -> None:
        """Check that a nested operation's ``request`` is already covered; acquire nothing.

        Raises ``StateError`` for a released set, another database or VM, or
        any entity of the request the set does not cover: an interior
        contract, since the caller built both requests.
        """
        if not self._active:
            raise StateError(f"a nested operation on VM '{request.vm}' received a released lock set")
        if database_path.resolve() != self.database_path or request.vm != self.vm:
            raise StateError(f"a nested operation on VM '{request.vm}' received the lock set of another VM or database")
        missing = [entity.key for entity, mode in request.modes().items() if not self.covers(entity, mode)]
        if missing:
            raise StateError(
                f"a nested operation on VM '{request.vm}' needs locks its caller does not hold: {', '.join(missing)}"
            )


_held_sets: ContextVar[tuple[NativeLockSet, ...]] = ContextVar("native_lock_held_sets", default=())
"""The sets the current invocation holds; another invocation in this process never sees them."""


@contextmanager
def native_locks(database_path: Path, request: LockRequest, *, prompts: bool = False) -> Iterator[NativeLockSet]:
    """Acquire ``request`` without waiting, hold it for the block, then release it.

    Raises :class:`NativeLockBusyError` at the first refusal, after
    releasing what this call took. Pass ``prompts=True`` only at the
    acquisitions sanctioned to prompt while held; any other prompt under a
    held set raises (:func:`prompt_notice`).
    """
    database_path = database_path.resolve()
    handles = _acquire(database_path, request, request.modes())
    try:
        lock_set = NativeLockSet(database_path, request, prompts=prompts)
        record = write_record(holders_directory(database_path, request.vm), lock_set.modes, acquired=time.time())
    except BaseException:
        _release(handles)
        raise
    _held_sets.set((*_held_sets.get(), lock_set))
    try:
        yield lock_set
    finally:
        lock_set._active = False
        _held_sets.set(tuple(held for held in _held_sets.get() if held is not lock_set))
        try:
            if record is not None:
                record.retire()
        finally:
            _release(handles)


def probe_locks(database_path: Path, request: LockRequest) -> None:
    """Try ``request`` without waiting and release it at once, writing no record.

    Raises :class:`NativeLockBusyError` like :func:`native_locks`, so a
    conflict fails before any prompt or boot. It decides nothing, since the
    real acquisition follows. ``path`` entities are left out: their names
    come from the guest walk, which runs later.
    """
    database_path = database_path.resolve()
    modes = {entity: mode for entity, mode in request.modes().items() if entity.kind is not EntityKind.PATH}
    _release(_acquire(database_path, request, modes))


def prompt_notice() -> None:
    """Called by every prompt helper before it prompts.

    Under sets acquired with ``prompts=True``, shows each set's notice once,
    before its first prompt. Under any other held set it raises
    ``StateError``: a prompt there is a programming error. With no set held
    it does nothing.
    """
    held = _held_sets.get()
    unsanctioned = [lock_set for lock_set in held if not lock_set.prompts]
    if unsanctioned:
        keys = ", ".join(entity.key for entity in unsanctioned[0].modes)
        raise StateError(f"a prompt ran while holding {keys} on VM '{unsanctioned[0].vm}', which may not prompt")
    for lock_set in held:
        if not lock_set._notice_shown:
            lock_set._notice_shown = True
            keys = ", ".join(entity.key for entity in lock_set.modes)
            held_on = f"{keys} on VM '{lock_set.vm}'"
            output.notice(
                f"Holding {held_on} until you answer; other operations that change them are refused meanwhile."
            )


def _acquire(database_path: Path, request: LockRequest, modes: Mapping[Entity, LockMode]) -> list[BinaryIO]:
    taken: list[BinaryIO] = []
    refused: tuple[Entity, LockMode] | None = None
    try:
        for entity, mode in modes.items():
            lock_file = open_lock_file(entity_lock_path(database_path, entity))
            try:
                locked = try_lock(lock_file.fileno(), mode)
            except BaseException:
                lock_file.close()
                raise
            if not locked:
                lock_file.close()
                refused = (entity, mode)
                break
            taken.append(lock_file)
    except BaseException:
        _release(taken)
        raise
    if refused is None:
        return taken
    _release(taken)
    entity, mode = refused
    holder = find_holder(holders_directory(database_path, request.vm), entity, mode)
    raise NativeLockBusyError(request, entity, mode, holder)


def _release(handles: list[BinaryIO]) -> None:
    """Unlock and close each handle in reverse order, closing every one even if an unlock fails."""
    with ExitStack() as stack:
        for lock_file in handles:
            stack.callback(lock_file.close)
            stack.callback(unlock, lock_file.fileno())
