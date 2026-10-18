"""Known-undispatched setup releases only its own mutation custody."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution._file_effect_gate_exchange import GateControlMutationUncertain
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_obligation import decode_file_call_obligation
from agentworks.execution.carrier import CarrierReport, Deadline, Dispatch, Failure
from agentworks.operations import LifecycleObligation, OperationBorrow
from tests.execution.files._file_publication_support import LocalCarrier
from tests.execution.files._file_upload_support import new_metadata
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files.test_file_operation_upload_gate import context as context
from tests.execution.files.test_remaining_effect_gates import _json_call
from tests.execution.files.test_remaining_effect_gates import effects as effects

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.execution._file_operation import FileOperation
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier


def _call_setup_case(
    operation: FileOperation,
    carrier: Carrier,
    root: Path,
    plan: IdentityPlan,
    setup: FileEffectGateSetup,
    case: str,
) -> None:
    from tests.execution.files.test_file_publication_protocol import _revision

    common = dict(
        trusted_root_path=str(root),
        relative_path="target",
        plan=plan,
        deadline=Deadline.after(10),
        runtime_selection=runtime_selection(sys.executable),
        gate_setup=setup,
    )
    metadata = new_metadata()
    if case == "json":
        _json_call(operation, carrier, root, plan, gate_setup=setup)
    elif case == "remove":
        operation.remove(carrier, **common, expected_kind=FileKind.REGULAR, expected_revision=_revision())
    elif case == "set_metadata":
        operation.set_metadata(
            carrier, **common, trusted_owner=metadata.owner, trusted_group=metadata.group, mode=0o600
        )
    elif case == "ensure_directory":
        operation.ensure_directory(
            carrier, **common, trusted_owner=metadata.owner, trusted_group=metadata.group, mode=0o700
        )
    else:
        assert case == "create_directory"
        operation.create_directory(
            carrier, **common, trusted_owner=metadata.owner, trusted_group=metadata.group, mode=0o700
        )


@pytest.mark.parametrize("case", ["json", "remove", "set_metadata", "ensure_directory", "create_directory"])
def test_known_not_sent_setup_releases_exact_call_and_allows_owner_completion(effects, case):
    database, owner, operation, root, plan, gate = effects
    os.unlink(gate.path)
    target = root / "target"
    original = b'{"initial":true}'
    if case in {"json", "remove", "set_metadata"}:
        target.write_bytes(original)
    receipt_ids: list[str] = []

    class NotSent(LocalCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            self.calls += 1
            assert custody is not None and custody.settled
            (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
            receipt_ids.append(row.obligation_id)
            assert decode_file_call_obligation(row.payload).gate_setup == FileEffectGateSetup(gate.path, gate.guest)
            return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)

    carrier = NotSent()
    with pytest.raises(StateError):
        _call_setup_case(operation, carrier, root, plan, FileEffectGateSetup(gate.path, gate.guest), case)
    assert not operation.active_json_updates
    assert not operation.active_removals
    assert not operation.active_metadata
    assert not owner.list_pending_lifecycle_obligations()
    (receipt_id,) = receipt_ids
    receipt = owner.inspect_lifecycle_obligation(receipt_id)
    assert receipt is not None and receipt.state is LifecycleObligationState.RESOLVED
    assert carrier.calls == 1 and not os.path.exists(gate.path)
    if case in {"json", "remove", "set_metadata"}:
        assert target.read_bytes() == original
    else:
        assert not target.exists()
    owner.borrow().close()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize("case", ["json", "remove", "set_metadata", "ensure_directory", "create_directory"])
@pytest.mark.parametrize("mode", ["unknown", "local_unsettled", "sent_incomplete"])
def test_uncertain_setup_retains_exact_active_call_and_borrow(effects, case, mode):
    database, owner, operation, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b'{"initial":true}')

    class Uncertain(LocalCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            if mode == "sent_incomplete":
                result = super().execute(invocation, io=io, deadline=deadline, custody=custody)
                return replace(result, stdout=replace(result.stdout, complete=False))
            self.calls += 1
            assert custody is not None
            if mode == "local_unsettled":
                custody.begin_process()
                assert not custody.settled
                return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)
            assert mode == "unknown"
            return CarrierReport(Dispatch.UNKNOWN, failure=Failure.DISPATCH)

    carrier = Uncertain()
    with pytest.raises(GateControlMutationUncertain):
        _call_setup_case(operation, carrier, root, plan, FileEffectGateSetup(gate.path, gate.guest), case)
    assert len(operation.active_json_updates + operation.active_removals + operation.active_metadata) == 1
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    call = decode_file_call_obligation(row.payload)
    assert call.gate_setup is not None and call.effect_gate is None
    assert carrier.calls == 1 and target.read_bytes() == b'{"initial":true}'
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()


@pytest.mark.parametrize("case", ["json", "remove", "set_metadata", "ensure_directory", "create_directory"])
def test_ambiguous_gate_publication_retains_call_and_original_interruption(effects, monkeypatch, case):
    database, owner, operation, root, plan, gate = effects
    target = root / "target"
    target.write_bytes(b'{"initial":true}')
    original = LifecycleObligation.publish_payload
    control = KeyboardInterrupt()

    def publish_then_interrupt(self, **kwargs):
        original(self, **kwargs)
        raise control

    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish_then_interrupt)
    carrier = LocalCarrier()
    with pytest.raises(KeyboardInterrupt) as raised:
        _call_setup_case(operation, carrier, root, plan, FileEffectGateSetup(gate.path, gate.guest), case)
    assert raised.value is control
    assert len(operation.active_json_updates + operation.active_removals + operation.active_metadata) == 1
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    call = decode_file_call_obligation(row.payload)
    assert call.effect_gate == gate and call.gate_setup is None
    assert carrier.calls == 1 and target.read_bytes() == b'{"initial":true}'
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("case", ["json", "remove", "set_metadata", "ensure_directory", "create_directory"])
def test_not_sent_borrow_close_interruption_keeps_exact_control_and_active_call(effects, monkeypatch, case):
    _, owner, operation, root, plan, gate = effects
    control = KeyboardInterrupt()
    seen = []

    class NotSent(LocalCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            self.calls += 1
            return CarrierReport(Dispatch.NOT_SENT, failure=Failure.DISPATCH)

    def interrupt_close(borrow):
        seen.append(borrow)
        raise control

    monkeypatch.setattr(OperationBorrow, "close", interrupt_close)
    carrier = NotSent()
    with pytest.raises(KeyboardInterrupt) as raised:
        _call_setup_case(operation, carrier, root, plan, FileEffectGateSetup(gate.path, gate.guest), case)
    assert raised.value is control
    assert len(seen) == 1 and not seen[0].closed
    active = operation.active_json_updates + operation.active_removals + operation.active_metadata
    assert len(active) == 1 and active[0].borrow is seen[0]
    assert carrier.calls == 1 and not (root / "target").exists()
    with pytest.raises(StateError):
        owner.borrow()
