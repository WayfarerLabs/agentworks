"""vm start and stop refused at their final set keep the intent they recorded and say how to restore it."""

from __future__ import annotations

from contextlib import ExitStack

import pytest

from agentworks.db import DesiredRuntimeState
from agentworks.native_locks import NativeLockBusyError, native_locks
from agentworks.native_locks.rows import vm_exclusive
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager import runtime as vm_runtime
from tests.runtime_state.runtime_support import REFUSE, seed_vm
from tests.runtime_state.runtime_support import runtime as runtime


@pytest.mark.parametrize(
    ("operation", "previous", "restore"),
    [
        ("stop", DesiredRuntimeState.RUNNING, ("agw", "vm", "set-desired-state", "box", "running")),
        ("stop", DesiredRuntimeState.STOPPED, None),
        ("start", DesiredRuntimeState.STOPPED, ("agw", "vm", "set-desired-state", "box", "stopped")),
    ],
    ids=["stop-running", "stop-unchanged", "start-stopped"],
)
def test_a_power_change_refused_after_recording_its_intent_says_how_to_restore_it(
    db, make_config, runtime, monkeypatch: pytest.MonkeyPatch, operation, previous, restore
) -> None:
    seed_vm(db, "box", target=previous)
    prepare = vm_runtime.prepare_vm_runtime
    with ExitStack() as other_operation:

        def prepare_then_another_operation_takes_the_vm(*args, **kwargs):
            # Between the brief intent write and the final set, another operation takes the VM.
            context = prepare(*args, **kwargs)
            other_operation.enter_context(native_locks(db.path, vm_exclusive("box")))
            return context

        monkeypatch.setattr(vm_runtime, "prepare_vm_runtime", prepare_then_another_operation_takes_the_vm)
        command = vm_manager.stop_vm if operation == "stop" else vm_manager.start_vm
        with pytest.raises(NativeLockBusyError) as refused:
            command(db, make_config(), "box", interaction=REFUSE)
    assert refused.value.restore_intent == restore
    recorded = DesiredRuntimeState.STOPPED if operation == "stop" else DesiredRuntimeState.RUNNING
    vm = db.get_vm("box")
    assert vm is not None and vm.desired_runtime_state is recorded
    assert ("vm-stop", "box") not in runtime[2] and ("vm-start", "box") not in runtime[2]
