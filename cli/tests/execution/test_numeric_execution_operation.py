"""Operation-bound inline bootstrap with simulated privileged admission.

Packed helper coverage reuses the existing admission double, not native root
credentials. Preparation, guest checks, body execution and custody remain real.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation, InlineExecutionControlFact
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline_bundle import ROOT_PROGRAM
from agentworks.execution._inline_request import decode_manifest
from agentworks.execution._managed_runs import ManagedTargetKind
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, Dispatch, FiniteInput, PreparedInvocation
from agentworks.execution.models import Command
from agentworks.execution.result import ApplicationState, ExitCode
from agentworks.operations import OperationOwner
from tests.execution.files._target_support import target_for_owner
from tests.execution.test_execution_operation import RecordingCarrier
from tests.execution.test_numeric_inline_bootstrap_adoption import _CONTEXT, _GUEST, _PackedCarrier, _plan

_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_LINUX = pytest.mark.skipif(sys.platform != "linux", reason="packed inline helper requires Linux")


@pytest.fixture
def owner(tmp_path: Path) -> Iterator[tuple[Database, OperationOwner]]:
    database = Database(tmp_path / "state.db")
    operation_owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "numeric-execution-vm"), "execution"
    )
    try:
        yield database, operation_owner
    finally:
        database.close()


def _operation(owner: OperationOwner, bootstrap: _NumericGuestBootstrap = _CONTEXT) -> ExecutionOperation:
    target = replace(target_for_owner(owner), boot_id=vm_guest_boot_id(bootstrap.guest))
    return ExecutionOperation(owner, target, bootstrap=bootstrap)


@pytest.mark.parametrize("mismatch", ["kind", "name", "boot", "init"])
def test_constructor_refuses_mismatch_before_borrow(
    owner: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    database, operation_owner = owner
    target = replace(target_for_owner(operation_owner), boot_id=vm_guest_boot_id(_GUEST))
    if mismatch == "kind":
        target = replace(target, kind=ManagedTargetKind.PLATFORM_HOST)
    elif mismatch == "name":
        target = replace(target, name="other-vm")
    elif mismatch == "boot":
        target = replace(target, boot_id=_GUEST.boot_id)
    else:
        target = replace(target, boot_id=vm_guest_boot_id(replace(_GUEST, init_start_ticks=1235)))

    def refuse_borrow(self: OperationOwner) -> None:
        pytest.fail("invalid constructor borrowed ownership")

    monkeypatch.setattr(OperationOwner, "borrow", refuse_borrow)
    with pytest.raises(ValidationError):
        ExecutionOperation(operation_owner, target, bootstrap=_CONTEXT)
    claim = database.operations.inspect(operation_owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    assert database.operations.list_lifecycle_obligations(operation_owner.ownership) == ()
    operation_owner.close()


def test_platform_host_accepts_context_free_operation_and_refuses_bootstrap(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    try:
        operation_owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.PLATFORM_HOST, "native-host"), "execution"
        )
        target = target_for_owner(operation_owner)
        ExecutionOperation(operation_owner, target)
        with pytest.raises(ValidationError):
            ExecutionOperation(operation_owner, target, bootstrap=_CONTEXT)
        assert database.operations.list_lifecycle_obligations(operation_owner.ownership) == ()
        operation_owner.close()
    finally:
        database.close()


class PreparedCarrier(RecordingCarrier):
    """Observe a real prepared invocation before returning explicit evidence."""

    def __init__(self, bootstrap: _NumericGuestBootstrap, body: IdentityPlan) -> None:
        super().__init__(CarrierReport(Dispatch.NOT_SENT))
        self.bootstrap = bootstrap
        self.body = body

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        super().validate(invocation, io=io)
        assert isinstance(io.input, FiniteInput)
        manifest = decode_manifest(io.input.data)
        assert manifest.identity == self.body.expected
        expected, _, _ = build_root_guest_bootstrap_argv(
            self.bootstrap.root_entry,
            self.body.expected,
            selection=_RUNTIME,
            program=ROOT_PROGRAM,
            nonce=manifest.nonce,
            expected_guest=self.bootstrap.guest,
        )
        assert invocation.argv == expected


def test_each_body_plan_stays_distinct_from_fixed_root_entry(owner: tuple[Database, OperationOwner]) -> None:
    _, operation_owner = owner
    bootstrap = replace(_CONTEXT, root_entry=replace(_CONTEXT.root_entry, mode=IdentityMode.SUDO_ROOT))
    operation = _operation(operation_owner, bootstrap)
    ordinary = IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DEMOTE)
    elevated = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
    for body in (ordinary, elevated):
        carrier = PreparedCarrier(bootstrap, body)
        outcome = operation.run_inline(
            carrier, Command(["/bin/true"]), plan=body, deadline=Deadline.after(10), runtime_selection=_RUNTIME
        )
        assert carrier.calls == 1
        assert not outcome.requires_owner_retention
    operation_owner.seal_lifecycle_obligations()
    operation_owner.record_effects_resolved()
    operation_owner.close()


@_LINUX
@pytest.mark.parametrize("damage", [None, "lost", "invalid"])
def test_real_preparation_and_packed_helper_preserve_terminal_limits(
    owner: tuple[Database, OperationOwner], damage: str | None
) -> None:
    _, operation_owner = owner
    operation = _operation(operation_owner)
    carrier = _PackedCarrier()
    carrier.damage = damage
    outcome = operation.run_inline(
        carrier,
        Command(["/bin/cat"]),
        plan=_plan(),
        stdin=b"payload\0\xff",
        deadline=Deadline.after(10),
        runtime_selection=_RUNTIME,
    )
    result = reduce_owned_inline_result(outcome)
    assert not outcome.requires_owner_retention
    if damage is None:
        assert result.application_state is ApplicationState.COMPLETED
        assert result.status == ExitCode(0) and result.stdout.data == b"payload\0\xff"
        assert result.owned_cleanup_confirmed
    else:
        assert result.application_state is ApplicationState.UNKNOWN and result.status is None
        assert not result.owned_cleanup_confirmed and not result.ok
    operation_owner.seal_lifecycle_obligations()
    operation_owner.record_effects_resolved()
    operation_owner.close()


@_LINUX
@pytest.mark.parametrize(
    "field,value",
    [("instance_marker", "b" * 32), ("boot_id", "223e4567-e89b-12d3-a456-426614174000"), ("init_start_ticks", 1235)],
)
def test_operation_forwards_full_guest_checkpoint_before_body(
    owner: tuple[Database, OperationOwner], tmp_path: Path, field: str, value: str | int
) -> None:
    _, operation_owner = owner
    operation = _operation(operation_owner)
    carrier = _PackedCarrier()
    carrier.observed = replace(_GUEST, **{field: value})
    body_marker = tmp_path / "body-ran"
    outcome = operation.run_inline(
        carrier,
        Command(["/usr/bin/touch", str(body_marker)]),
        plan=_plan(),
        deadline=Deadline.after(10),
        runtime_selection=_RUNTIME,
    )
    assert "request" not in carrier.events and not body_marker.exists()
    result = reduce_owned_inline_result(outcome)
    assert result.application_state is ApplicationState.UNKNOWN and not result.ok
    assert outcome.requires_owner_retention
    with pytest.raises(StateError):
        operation_owner.close()


@pytest.mark.parametrize("control", [False, True])
def test_uncertain_dispatch_retains_safe_operation_custody(
    owner: tuple[Database, OperationOwner], control: bool
) -> None:
    database, operation_owner = owner
    operation = _operation(operation_owner)
    interruption = KeyboardInterrupt()
    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT), control=interruption if control else None)
    if control:
        with pytest.raises(KeyboardInterrupt) as caught:
            operation.run_inline(
                carrier,
                Command(["/bin/true"]),
                plan=_plan(),
                deadline=Deadline.after(10),
                runtime_selection=_RUNTIME,
            )
        assert caught.value is interruption
        fact = caught.value.__cause__
        assert isinstance(fact, InlineExecutionControlFact)
        outcome = fact.outcome
        assert outcome.candidate is None
    else:
        outcome = operation.run_inline(
            carrier,
            Command(["/bin/true"]),
            plan=_plan(),
            deadline=Deadline.after(10),
            runtime_selection=_RUNTIME,
        )
        assert outcome.candidate is not None
    assert carrier.calls == 1 and outcome.requires_owner_retention and outcome.pending_remote_effects
    assert operation.active_inline_calls == ()
    (unfinished,) = operation.unfinished_inline_executions
    assert unfinished.outcome.candidate is None and unfinished.outcome.requires_owner_retention
    result = reduce_owned_inline_result(outcome)
    assert result.application_state is ApplicationState.UNKNOWN and not result.owned_cleanup_confirmed
    claim = database.operations.inspect(operation_owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        operation_owner.close()
