"""vm start and stop refused at their final set, busy or changed, keep their recorded intent and say how to undo it."""

from __future__ import annotations

from contextlib import ExitStack

import pytest

from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateConflictError
from agentworks.native_locks import NativeLockBusyError, native_locks
from agentworks.native_locks.rows import vm_exclusive
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager import runtime as vm_runtime
from tests.runtime_state.runtime_support import REFUSE, seed_vm
from tests.runtime_state.runtime_support import runtime as runtime

pytestmark = pytest.mark.windows


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


@pytest.mark.parametrize(
    ("operation", "previous", "restore"),
    [
        ("stop", DesiredRuntimeState.RUNNING, ("agw", "vm", "set-desired-state", "box", "running")),
        ("start", DesiredRuntimeState.STOPPED, ("agw", "vm", "set-desired-state", "box", "stopped")),
    ],
    ids=["stop-running", "start-stopped"],
)
def test_a_power_change_whose_vm_changed_before_its_final_set_refuses_and_says_how_to_restore_it(
    db, make_config, runtime, monkeypatch: pytest.MonkeyPatch, operation, previous, restore
) -> None:
    seed_vm(db, "box", target=previous)
    prepare = vm_runtime.prepare_vm_runtime

    def prepare_then_the_vm_is_readdressed(*args, **kwargs):
        # A rekey or recover lands after the preparation admitted the row and before the final set.
        context = prepare(*args, **kwargs)
        db.update_vm_tailscale("box", "100.64.0.77")
        return context

    monkeypatch.setattr(vm_runtime, "prepare_vm_runtime", prepare_then_the_vm_is_readdressed)
    command = vm_manager.stop_vm if operation == "stop" else vm_manager.start_vm
    with pytest.raises(StateConflictError) as refused:
        command(db, make_config(), "box", interaction=REFUSE)
    assert refused.value.restore_intent == restore
    assert ("vm-stop", "box") not in runtime[2] and ("vm-start", "box") not in runtime[2]


@pytest.mark.parametrize("operation", ["stop", "start"])
def test_a_power_change_whose_intent_another_command_replaced_gives_no_restore_words(
    db, make_config, runtime, monkeypatch: pytest.MonkeyPatch, operation
) -> None:
    previous = DesiredRuntimeState.RUNNING if operation == "stop" else DesiredRuntimeState.STOPPED
    seed_vm(db, "box", target=previous)
    prepare = vm_runtime.prepare_vm_runtime

    def prepare_then_another_command_records_the_old_intent(*args, **kwargs):
        # Another command puts the intent back after the brief write, so nothing of ours stands to restore.
        context = prepare(*args, **kwargs)
        vm = db.get_vm("box")
        db.set_vm_desired_runtime_state("box", previous, expected_vm_uuid=vm.vm_uuid)
        return context

    monkeypatch.setattr(vm_runtime, "prepare_vm_runtime", prepare_then_another_command_records_the_old_intent)
    command = vm_manager.stop_vm if operation == "stop" else vm_manager.start_vm
    with pytest.raises(StateConflictError) as refused:
        command(db, make_config(), "box", interaction=REFUSE)
    assert refused.value.restore_intent is None


@pytest.mark.parametrize("operation", ["stop", "start"])
def test_a_power_change_whose_vm_was_replaced_under_its_name_gives_no_restore_words(
    db, make_config, runtime, monkeypatch: pytest.MonkeyPatch, operation
) -> None:
    previous = DesiredRuntimeState.RUNNING if operation == "stop" else DesiredRuntimeState.STOPPED
    recorded = DesiredRuntimeState.STOPPED if operation == "stop" else DesiredRuntimeState.RUNNING
    seed_vm(db, "box", target=previous)
    prepare = vm_runtime.prepare_vm_runtime

    def prepare_then_the_vm_is_replaced(*args, **kwargs):
        # After the brief write, another VM takes the name, with the intent this command recorded.
        context = prepare(*args, **kwargs)
        db.delete_vm("box")
        seed_vm(db, "box", target=recorded)
        return context

    monkeypatch.setattr(vm_runtime, "prepare_vm_runtime", prepare_then_the_vm_is_replaced)
    command = vm_manager.stop_vm if operation == "stop" else vm_manager.start_vm
    with pytest.raises(StateConflictError) as refused:
        command(db, make_config(), "box", interaction=REFUSE)
    assert refused.value.restore_intent is None
    assert ("vm-stop", "box") not in runtime[2] and ("vm-start", "box") not in runtime[2]
