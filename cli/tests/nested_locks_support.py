"""Nested operations called directly by tests, under the lock set their command would hold around them.

A nested operation (harness setup, workspace or agent realization, VM
initialization) acquires nothing: it requires its request of the caller's
set. A test that calls one directly takes that set first, through these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.native_locks import native_locks

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agentworks.db import Database
    from agentworks.harness_setup.model import NativeSetupState
    from agentworks.native_locks import NativeLockSet


def run_setup_held(db: Database, *args: Any, **kwargs: Any) -> NativeSetupState:
    """``harness_setup.dispatch.run_setup`` under its owner's lock (``setup_request``)."""
    from agentworks.harness_setup.dispatch import run_setup, setup_request

    invocation = args[2] if len(args) > 2 else kwargs["invocation"]
    with native_locks(db.path, setup_request(invocation)) as held:
        return run_setup(db, *args, held=held, **kwargs)


def holding_vm(db: Database, vm_name: str) -> AbstractContextManager[NativeLockSet]:
    """The vm exclusive, which covers every nested operation on the VM, as a VM-wide command holds it."""
    from agentworks.native_locks.rows import vm_exclusive

    return native_locks(db.path, vm_exclusive(vm_name))
