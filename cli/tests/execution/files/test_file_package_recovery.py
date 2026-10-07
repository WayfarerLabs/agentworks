"""Fence one retained package child without settling or replaying it."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.execution import (
    _file_effect_gate,
    _file_effect_gate_exchange,
    _file_gate_setup,
    _file_package_recovery,
    _process,
)
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_effect_gate import FileEffectGateError, hold_file_effect_gate
from agentworks.execution._file_effect_gate_bundle import _MODULE_NAMES, _PACKAGE
from agentworks.execution._file_effect_gate_exchange import exchange_file_effect_gate
from agentworks.execution._file_gate_control import setup_file_effect_gate
from agentworks.execution._file_gate_setup import file_effect_gate_path
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_admission,
    encode_file_call_obligation,
)
from agentworks.execution._file_package_recovery import FilePackageFenceRecovery
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, Failure, PreparedInvocation
from agentworks.operations import OperationOwner
from tests.execution._bound_carrier_support import hold_operation_owner as hold_operation_owner
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file gate requires Linux")
_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    namespace = tmp_path / "run" / "agentworks" / "file-gates-v1"
    namespace.mkdir(parents=True)
    monkeypatch.setattr(_file_gate_setup, "_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_GATE_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_ROOT_UID", os.geteuid())
    patch = (
        "import os\n"
        f"gate=sys.modules[{(_PACKAGE + '._file_effect_gate')!r}]\n"
        f"gate._GATE_NAMESPACE={str(namespace)!r}\n"
        "gate._ROOT_UID=os.geteuid()\n"
        f"guest._identity=lambda: sys.modules[{(_PACKAGE + '._vm_guest_identity_protocol')!r}]."
        f"VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})\n"
    )
    monkeypatch.setattr(
        _file_effect_gate_exchange,
        "FIXED_BUNDLE",
        fixture_file_bundle(_PACKAGE, _MODULE_NAMES, "_file_effect_gate_guest", patch),
    )
    database = Database(tmp_path / "owner.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "package-vm"), "package"
    )
    target = replace(target_for_owner(owner), boot_id=vm_guest_boot_id(_GUEST))
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    path = file_effect_gate_path(target, os.geteuid(), _GUEST)
    Path(path).parent.mkdir(mode=0o700)
    gate = setup_file_effect_gate(path, _GUEST, os.geteuid(), target.name, lambda: _GUEST)
    try:
        yield database, owner, target, plan, gate
    finally:
        assert owner.close_local_delivery(Deadline.after(3))
        database.close()


def _admit(context, index: int):
    database, owner, target, plan, gate = context
    call = FileCallObligation(
        family=FileCallFamily.PACKAGE_UPLOAD,
        target=target,
        root="/srv/package",
        relative_path=f"member-{index}",
        identity_plan=plan,
        runtime_selection=runtime_selection(sys.executable),
        token=b"t" * 16,
        batch_index=index,
        effect_gate=gate,
    )
    obligation = owner.register_lifecycle_obligation(
        "file-call", payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION, payload=encode_file_call_admission(call)
    )
    obligation.mark_possible_effect()
    owner.seal_lifecycle_obligations()
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    return call, row


@pytest.fixture
def held_recovery_owner(context, request: pytest.FixtureRequest):
    # Acquire the existing holder after context setup so its teardown precedes
    # database closure, without maintaining another registry of owners.
    return request.getfixturevalue("hold_operation_owner")


def _recover(
    context, row, generation: str, *, hold_owner: Callable[[OperationOwner], OperationOwner]
) -> tuple[OperationOwner, FilePackageFenceRecovery]:
    database, owner, target, _, _ = context
    recovered = hold_owner(OperationOwner.recover(database.operations, owner.ownership, generation))
    return recovered, FilePackageFenceRecovery.open(recovered, target, row)


def test_recovery_holder_receives_original_owner_before_open_and_retains_actual_worker(
    context, held_recovery_owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, row = _admit(context, 3)
    held_owner: OperationOwner | None = None
    original_open = FilePackageFenceRecovery.open

    def hold(owner: OperationOwner) -> OperationOwner:
        nonlocal held_owner
        held_recovery_owner(owner)
        held_owner = owner
        return owner

    def open_held(owner, target, retained):
        assert owner is held_owner
        return original_open(owner, target, retained)

    monkeypatch.setattr(FilePackageFenceRecovery, "open", open_held)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=hold)
    assert held_owner is not None
    assert recovered is held_owner
    try:
        with monkeypatch.context() as fault:
            fault.setattr(_process, "_cleanup", lambda status: False)
            result = fence.advance(LocalCarrier(), deadline=Deadline.after(30))
            assert result.carrier_failure is Failure.OBSERVATION
            assert not recovered.close_local_delivery(Deadline.after(3))
    finally:
        assert recovered.close_local_delivery(Deadline.after(3))


@pytest.mark.parametrize("index", [0, 7])
def test_advance_fences_old_guest_effect_and_preserves_child(context, held_recovery_owner, index: int) -> None:
    database, owner, _, _, gate = context
    call, row = _admit(context, index)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=held_recovery_owner)
    result = fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    assert result.observation is not None and result.observation.binding is not None
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    retained = decode_file_call_obligation(current.payload)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert replace(retained, effect_gate=gate) == call
    assert retained.effect_gate == result.observation.binding
    assert retained.effect_gate != gate
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(gate, lambda: _GUEST):
        pass
    with hold_file_effect_gate(retained.effect_gate, lambda: _GUEST):
        pass
    with pytest.raises(StateError):
        recovered.close()


def test_lost_advance_reply_reuses_durable_proposal(held_recovery_owner, context) -> None:
    database, owner, target, _, gate = context
    _, row = _admit(context, 3)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=held_recovery_owner)

    class LostReply(LocalCarrier):
        def execute(
            self,
            invocation: PreparedInvocation,
            *,
            io: CarrierIO,
            deadline: Deadline,
            custody: LocalDeliveryCustody | None = None,
        ) -> CarrierReport:
            super().execute(invocation, io=io, deadline=deadline, custody=custody)
            raise RuntimeError("lost advance reply")

    with pytest.raises(RuntimeError, match="lost advance reply"):
        fence.advance(LostReply(), deadline=Deadline.after(30))
    (proposed_row,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    proposed = decode_file_call_obligation(proposed_row.payload).effect_gate
    assert proposed is not None and proposed.proposed_generation is not None
    again = held_recovery_owner(OperationOwner.recover(database.operations, recovered.ownership, "c" * 32))
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    result = FilePackageFenceRecovery.open(again, target, current).advance(LocalCarrier(), deadline=Deadline.after(30))
    assert result.observation is not None
    assert result.observation.binding == replace(
        proposed, generation=proposed.proposed_generation, proposed_generation=None
    )
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    assert decode_file_call_obligation(current.payload).effect_gate == result.observation.binding
    assert decode_file_call_obligation(current.payload).effect_gate != gate
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT


def test_lost_proposal_publication_reply_does_not_choose_another_generation(
    held_recovery_owner, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, target, _, _ = context
    _, row = _admit(context, 1)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=held_recovery_owner)
    original = type(database.operations).publish_lifecycle_obligation_payload

    def lost_reply(repository, ownership, obligation_id, *, expected_revision, payload_version, payload):
        original(
            repository,
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )
        raise RuntimeError("lost proposal reply")

    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", lost_reply)
    with pytest.raises(RuntimeError, match="lost proposal reply"):
        fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    (proposed_row,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    proposed = decode_file_call_obligation(proposed_row.payload).effect_gate
    assert proposed is not None and proposed.proposed_generation is not None
    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", original)
    again = held_recovery_owner(OperationOwner.recover(database.operations, recovered.ownership, "c" * 32))
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    FilePackageFenceRecovery.open(again, target, current).advance(LocalCarrier(), deadline=Deadline.after(30))
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    confirmed = decode_file_call_obligation(current.payload).effect_gate
    assert confirmed is not None and confirmed.generation == proposed.proposed_generation


def test_stale_row_and_missing_gate_refuse_without_resolution(held_recovery_owner, context) -> None:
    database, owner, target, _, gate = context
    call, row = _admit(context, 1)
    recovered = held_recovery_owner(OperationOwner.recover(database.operations, owner.ownership, "b" * 32))
    bound = recovered.rebind_lifecycle_obligation(
        row.obligation_id, "file-call", payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION, payload=row.payload
    )
    bound.publish_payload(
        expected_revision=row.payload_revision,
        payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
        payload=encode_file_call_obligation(replace(call, relative_path="changed")),
    )
    with pytest.raises(StateError):
        FilePackageFenceRecovery.open(recovered, target, row)
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    fence = FilePackageFenceRecovery.open(recovered, target, current)
    Path(gate.path).unlink()
    result = fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    assert result.observation is None or result.observation.binding is None
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    proposed = decode_file_call_obligation(current.payload).effect_gate
    assert proposed is not None and proposed.proposed_generation is not None


def test_wrong_guest_and_replaced_gate_refuse(held_recovery_owner, context) -> None:
    database, owner, target, _, gate = context
    _, row = _admit(context, 2)
    recovered = held_recovery_owner(OperationOwner.recover(database.operations, owner.ownership, "b" * 32))
    wrong_target = replace(target, boot_id="123e4567-e89b-12d3-a456-426614174001")
    with pytest.raises(StateError):
        FilePackageFenceRecovery.open(recovered, wrong_target, row)
    fence = FilePackageFenceRecovery.open(recovered, target, row)
    Path(gate.path).unlink()
    setup_file_effect_gate(gate.path, _GUEST, os.geteuid(), target.name, lambda: _GUEST)
    result = fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    assert result.observation is None or result.observation.binding is None
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    proposed = decode_file_call_obligation(current.payload).effect_gate
    assert proposed is not None and proposed.proposed_generation is not None


def test_ungated_package_row_is_not_a_fence_candidate(held_recovery_owner, context) -> None:
    database, owner, target, _, _ = context
    call, row = _admit(context, 0)
    recovered = held_recovery_owner(OperationOwner.recover(database.operations, owner.ownership, "b" * 32))
    bound = recovered.rebind_lifecycle_obligation(
        row.obligation_id, "file-call", payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION, payload=row.payload
    )
    bound.publish_payload(
        expected_revision=row.payload_revision,
        payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
        payload=encode_file_call_obligation(replace(call, effect_gate=None)),
    )
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    with pytest.raises(StateError):
        FilePackageFenceRecovery.open(recovered, target, current)


def test_control_stop_after_proposal_reuses_it_on_next_takeover(
    held_recovery_owner, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, target, _, _ = context
    _, row = _admit(context, 4)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=held_recovery_owner)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("interrupted after proposal")

    original = exchange_file_effect_gate
    monkeypatch.setattr(_file_package_recovery, "exchange_file_effect_gate", interrupted)
    with pytest.raises(KeyboardInterrupt, match="interrupted after proposal"):
        fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    proposed = decode_file_call_obligation(current.payload).effect_gate
    assert proposed is not None and proposed.proposed_generation is not None
    monkeypatch.setattr(_file_package_recovery, "exchange_file_effect_gate", original)
    again = held_recovery_owner(OperationOwner.recover(database.operations, recovered.ownership, "c" * 32))
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    FilePackageFenceRecovery.open(again, target, current).advance(LocalCarrier(), deadline=Deadline.after(30))
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    confirmed = decode_file_call_obligation(current.payload).effect_gate
    assert confirmed is not None and confirmed.generation == proposed.proposed_generation


def test_lost_confirmed_binding_publication_keeps_resolved_gate(
    held_recovery_owner, context, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, target, _, gate = context
    _, row = _admit(context, 5)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=held_recovery_owner)
    original = type(database.operations).publish_lifecycle_obligation_payload

    def lost_confirmation(repository, ownership, obligation_id, *, expected_revision, payload_version, payload):
        result = original(
            repository,
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )
        call = decode_file_call_obligation(payload)
        if call.effect_gate is not None and call.effect_gate.proposed_generation is None:
            raise RuntimeError("lost confirmation reply")
        return result

    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", lost_confirmation)
    with pytest.raises(RuntimeError, match="lost confirmation reply"):
        fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    (current,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    call = decode_file_call_obligation(current.payload)
    assert call.effect_gate is not None and call.effect_gate.proposed_generation is None
    assert call.effect_gate != gate
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", original)
    again = held_recovery_owner(OperationOwner.recover(database.operations, recovered.ownership, "c" * 32))
    (current,) = database.operations.list_pending_lifecycle_obligations(again.ownership)
    assert decode_file_call_obligation(current.payload).effect_gate == call.effect_gate


def test_confirmed_binding_publication_refused_before_commit_recovers_without_redispatch(
    context, held_recovery_owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, _, _, gate = context
    _, row = _admit(context, 6)
    recovered, fence = _recover(context, row, "b" * 32, hold_owner=held_recovery_owner)
    original = type(database.operations).publish_lifecycle_obligation_payload
    failed = False

    def refuse_once(repository, ownership, obligation_id, *, expected_revision, payload_version, payload):
        nonlocal failed
        call = decode_file_call_obligation(payload)
        if not failed and call.effect_gate is not None and call.effect_gate.proposed_generation is None:
            failed = True
            raise RuntimeError("confirmation refused before commit")
        return original(
            repository,
            ownership,
            obligation_id,
            expected_revision=expected_revision,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(type(database.operations), "publish_lifecycle_obligation_payload", refuse_once)
    with pytest.raises(RuntimeError, match="confirmation refused before commit"):
        fence.advance(LocalCarrier(), deadline=Deadline.after(30))
    (proposed_row,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    proposed_gate = decode_file_call_obligation(proposed_row.payload).effect_gate
    assert proposed_gate is not None and proposed_gate.proposed_generation is not None
    carrier = LocalCarrier()
    with pytest.raises(StateError):
        fence.advance(carrier, deadline=Deadline.after(30))
    assert carrier.calls == 0
    (confirmed_row,) = database.operations.list_pending_lifecycle_obligations(recovered.ownership)
    confirmed_gate = decode_file_call_obligation(confirmed_row.payload).effect_gate
    assert confirmed_row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert confirmed_gate == replace(
        proposed_gate, generation=proposed_gate.proposed_generation, proposed_generation=None
    )
    assert confirmed_gate != gate
