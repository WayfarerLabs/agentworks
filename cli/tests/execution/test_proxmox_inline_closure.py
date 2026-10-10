"""Offline exact-ACK QGA continuation through actual prepared DIRECT helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation, InlineExecutionControlFact, OwnedInlineOutcome
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline_observer import InlineObserver
from agentworks.execution._runtime_prerequisite import (
    HelperClosureExpectation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.carriers._proxmox_helper_delivery import ProxmoxHelperDelivery
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection, _ProxmoxWire
from agentworks.execution.models import Command
from agentworks.execution.result import ApplicationState
from agentworks.operations import OperationAttempt, OperationBorrow, OperationOwner
from tests.execution.files._target_support import target_for_owner

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux inline helper")


@dataclass
class QGA:
    clock: list[float]
    deadline: Deadline
    running: bool = True
    fault: str | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    terminal: dict[str, object] = field(default_factory=dict)

    def request(
        self,
        method: str,
        suffix: str,
        *,
        custody: LocalDeliveryCustody,
        timeout: float | None,
        body: bytes | None = None,
    ) -> dict[str, object]:
        assert timeout is not None and timeout > 0 and custody.settled
        self.calls.append((method, suffix))
        if method == "POST":
            assert suffix == "exec" and body is not None
            request = json.loads(body)
            result = subprocess.run(
                request["command"], input=request["input-data"].encode("ascii"), capture_output=True, timeout=15
            )
            assert result.returncode == 0
            self.terminal = {"exited": True, "exitcode": 0, "out-data": result.stdout.decode("ascii")}
            if self.fault == "lost-ack":
                raise OSError("lost acknowledgment")
            return {"pid": 71}
        assert suffix == "exec-status?pid=71"
        if self.running:
            self.running = False
            assert self.deadline.expires_at is not None
            self.clock[0] = self.deadline.expires_at + 1
            return {"exited": False}
        if self.fault == "get-failure":
            raise OSError("status exchange failed")
        status = self.terminal.copy()
        if self.fault == "wrong-nonce":
            status["out-data"] = "AGW_RUNTIME_1:" + "0" * 32 + ":ready:0\n"
        elif self.fault == "missing-ready":
            status["out-data"] = ""
        elif self.fault == "missing-status":
            return {}
        elif self.fault == "nonzero":
            status["exitcode"] = 1
        elif self.fault == "signal":
            del status["exitcode"]
            status["signal"] = 15
        return status


type Scenario = tuple[
    Database,
    OperationOwner,
    ExecutionOperation,
    ProxmoxCarrier,
    IdentityPlan,
    RuntimeSelection,
    QGA,
    list[ProxmoxHelperDelivery],
]


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scenario]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "closure-vm"), "inline-closure"
    )
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: clock[0])
    deadline = Deadline.after(1)
    qga = QGA(clock, deadline)
    monkeypatch.setattr(_ProxmoxWire, "request", lambda _self, *args, **kwargs: qga.request(*args, **kwargs))
    connection = ProxmoxConnection("https://pve.test", "node", 101, "token", "secret-token")
    base = ProxmoxCarrier(connection)
    deliveries: list[ProxmoxHelperDelivery] = []

    def factory(expectation: HelperClosureExpectation) -> ProxmoxHelperDelivery:
        delivery = ProxmoxHelperDelivery(connection, expectation)
        assert not qga.calls
        deliveries.append(delivery)
        return delivery

    runtime = RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
    binding = NativeExecutionBinding(base, "root", runtime, _new_helper_delivery=factory)
    operation = ExecutionOperation(owner, target_for_owner(owner), native_binding=binding)
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    try:
        yield database, owner, operation, base, plan, runtime, qga, deliveries
    finally:
        database.close()


def _run(scenario: Scenario) -> OwnedInlineOutcome:
    _, _, operation, base, plan, runtime, qga, _ = scenario
    return operation.run_inline(
        base,
        Command(("/bin/echo", "application-secret")),
        plan=plan,
        runtime_selection=runtime,
        deadline=qga.deadline,
        stdin=b"input-secret",
        env={"SECRET": "environment-secret"},
        sensitive=True,
    )


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("committed", [False, True])
def test_actual_nondispatch_lost_settle_reply_reconciles_without_runtime_or_budget(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, tracked: bool, committed: bool
) -> None:
    _, owner, operation, _, _, _, qga, deliveries = scenario
    assert operation._native_binding is not None
    if not tracked:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=None)
    execute = ProxmoxCarrier._execute
    settle = OperationAttempt.settle
    attempts: list[OperationAttempt] = []
    cause = ValueError("original cause")
    control = KeyboardInterrupt("settlement interrupted")
    control.__cause__ = cause

    def expire_then_execute(self, *args, **kwargs):
        assert qga.deadline.expires_at is not None
        qga.clock[0] = qga.deadline.expires_at + 1
        report = execute(self, *args, **kwargs)
        assert report.dispatch is Dispatch.NOT_SENT
        return report

    def interrupt_settlement(self):
        attempts.append(self)
        if committed:
            settle(self)
        raise control

    monkeypatch.setattr(ProxmoxCarrier, "_execute", expire_then_execute)
    monkeypatch.setattr(OperationAttempt, "settle", interrupt_settlement)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(scenario)
    assert caught.value is control and isinstance(control.__cause__, InlineExecutionControlFact)
    assert control.__cause__.__cause__ is cause
    (active,) = operation.active_inline_calls
    (attempt,) = attempts
    (obligation,) = owner.list_pending_lifecycle_obligations()
    assert obligation.obligation_id == operation._dispatch_id
    assert obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert active.operation.outstanding_attempt is attempt
    assert owner._outstanding_attempt is (None if committed else attempt)
    assert not active.borrow._closed and active.borrow.has_outstanding_attempt is (not committed)
    assert attempt.local_delivery.settled and not qga.calls
    if tracked:
        assert active.not_sent and not deliveries[0].closure_proven
        assert active.prepared is None and active.candidate is None
        assert active.outcome is not None and active.outcome.candidate is None
    assert reduce_owned_inline_result(control.__cause__.outcome).application_state is ApplicationState.UNKNOWN
    monkeypatch.setattr(OperationAttempt, "settle", settle)
    operation._route_check = lambda _deadline: pytest.fail("provider observation")
    operation.observe_inline_cleanup(Deadline.after(0))
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert not operation.active_inline_calls and not owner.list_pending_lifecycle_obligations() and not qga.calls


def test_actual_nondispatch_preserves_returned_unknown_without_runtime_proof(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, operation, _, _, _, qga, deliveries = scenario
    execute = ProxmoxCarrier._execute

    def expire_then_execute(self, *args, **kwargs):
        assert qga.deadline.expires_at is not None
        qga.clock[0] = qga.deadline.expires_at + 1
        return execute(self, *args, **kwargs)

    monkeypatch.setattr(ProxmoxCarrier, "_execute", expire_then_execute)
    outcome = _run(scenario)
    result = reduce_owned_inline_result(outcome)
    assert result.dispatch is Dispatch.NOT_SENT and result.application_state is ApplicationState.UNKNOWN
    assert not deliveries[0].closure_proven and not operation.active_inline_calls
    operation.observe_inline_cleanup(Deadline.after(0))
    operation.finish()
    assert result.application_state is ApplicationState.UNKNOWN and not qga.calls


def test_nondispatch_pending_local_cleanup_requires_only_finite_local_budget(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, operation, _, _, _, qga, deliveries = scenario
    execute = ProxmoxCarrier._execute
    settle = OperationAttempt.settle
    control = KeyboardInterrupt("before settlement")

    def expire_then_execute(self, *args, **kwargs):
        assert qga.deadline.expires_at is not None
        qga.clock[0] = qga.deadline.expires_at + 1
        return execute(self, *args, **kwargs)

    def interrupt(_self):
        raise control

    monkeypatch.setattr(ProxmoxCarrier, "_execute", expire_then_execute)
    monkeypatch.setattr(OperationAttempt, "settle", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(scenario)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    assert attempt is not None and active.not_sent and not deliveries[0].closure_proven
    attempt.local_delivery.begin_process()
    operation._route_check = lambda _deadline: pytest.fail("provider observation")
    monkeypatch.setattr(OperationAttempt, "settle", settle)
    with pytest.raises(ValidationError):
        operation.observe_inline_cleanup(Deadline.after(0))
    assert active.operation.outstanding_attempt is attempt and not attempt.local_delivery.settled
    operation.observe_inline_cleanup(Deadline.after(2))
    operation.finish()
    assert attempt.local_delivery.settled and not operation.active_inline_calls and not qga.calls


def test_lost_begin_reply_before_carrier_entry_keeps_original_nondelivery_reconciliation(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner, operation, _, _, _, qga, _ = scenario
    begin = OperationBorrow.begin_attempt
    attempts: list[OperationAttempt] = []
    control = KeyboardInterrupt("begin reply lost")

    def begin_then_interrupt(self):
        attempts.append(begin(self))
        raise control

    monkeypatch.setattr(OperationBorrow, "begin_attempt", begin_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(scenario)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    (attempt,) = attempts
    assert owner._outstanding_attempt is attempt and active.operation.outstanding_attempt is None
    assert not active.not_sent and active.prepared is None and active.candidate is None
    operation.observe_inline_cleanup(Deadline.after(0))
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations() and not operation.active_inline_calls and not qga.calls


@pytest.mark.parametrize("committed", [False, True])
def test_final_handoff_interruption_detaches_payload_and_retires_same_call(
    scenario, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    _, _, operation, _, _, _, qga, deliveries = scenario
    qga.running = False
    control = KeyboardInterrupt("handoff reply lost")
    cause = ValueError("original cause")
    control.__cause__ = cause
    original = OperationBorrow.handoff_retained_effect

    def interrupt(self):
        if committed:
            original(self)
        raise control

    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(scenario)
    assert caught.value is control and control.__cause__ is cause
    (active,) = operation.active_inline_calls
    assert active.bookkeeping_retained and deliveries[0].closure_proven
    assert active.prepared is None and active.candidate is None
    assert active.outcome is not None and active.outcome.candidate is None
    assert active.operation.outstanding_attempt is None and not active.borrow.has_outstanding_attempt
    assert active.borrow._closed is committed
    calls = list(qga.calls)
    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", original)
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert not operation.active_inline_calls and qga.calls == calls


@pytest.mark.parametrize("pending_local", [False, True])
def test_proven_closure_needs_local_cleanup_but_no_new_provider_observation(
    scenario, monkeypatch: pytest.MonkeyPatch, pending_local: bool
) -> None:
    _, _, operation, _, _, _, qga, deliveries = scenario
    qga.running = False
    control = KeyboardInterrupt("output interrupted")

    def interrupt(*_args):
        raise control

    monkeypatch.setattr(InlineObserver, "accept", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(scenario)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    assert attempt is not None and deliveries[0].closure_proven
    assert operation._native_binding is not None
    operation._route_check = lambda _deadline: pytest.fail("provider observation")
    calls = list(qga.calls)
    if pending_local:
        attempt.local_delivery.begin_process()
        with pytest.raises(ValidationError):
            operation.observe_inline_cleanup(Deadline.after(0))
        assert not attempt.local_delivery.settled
        operation.observe_inline_cleanup(Deadline.after(2))
    else:
        operation.observe_inline_cleanup(Deadline.after(0))
    operation.finish()
    assert attempt.local_delivery.settled and not operation.active_inline_calls and qga.calls == calls


@pytest.mark.parametrize("unusable", [False, True])
def test_generated_runtime_refusal_closes_only_original_invocation(scenario, tmp_path: Path, unusable: bool) -> None:
    _, owner, operation, base, plan, _, qga, deliveries = scenario
    runtime = tmp_path / "runtime"
    if unusable:
        runtime.write_bytes(b"not executable")
        runtime.chmod(0o600)
    qga.running = False
    effect = tmp_path / "application-effect"
    outcome = operation.run_inline(
        base,
        Command(("/usr/bin/touch", str(effect))),
        plan=plan,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, str(runtime)),
        deadline=qga.deadline,
    )
    assert outcome.candidate is not None
    assert outcome.candidate.runtime_prerequisite.state is (
        RuntimePrerequisiteState.UNUSABLE if unusable else RuntimePrerequisiteState.MISSING
    )
    result = reduce_owned_inline_result(outcome)
    assert result.application_state is ApplicationState.NOT_STARTED and not effect.exists()
    assert deliveries[0].closure_proven and not operation.active_inline_calls
    calls = list(qga.calls)
    operation.observe_inline_cleanup(Deadline.after(0))
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations() and qga.calls == calls
    assert result.application_state is ApplicationState.NOT_STARTED


@pytest.mark.parametrize(
    ("state", "token", "shim"),
    [
        ("missing", "-", None),
        ("unusable", "0", None),
        ("shim", "s", "/usr/bin/python3"),
        ("unsupported_version", "0", None),
        ("missing_modules", "0", None),
    ],
)
@pytest.mark.parametrize("fault", [None, "nonce", "malformed", "nonzero", "signal"])
def test_authenticated_closed_refusals_require_strict_prefix_and_independent_zero(state, token, shim, fault) -> None:
    nonce = "a" * 32
    delivery = ProxmoxHelperDelivery(
        ProxmoxConnection("https://pve.test", "node", 101, "token", "secret"),
        HelperClosureExpectation(nonce, ("/usr/bin/python3",), shim, None),
    )
    received_nonce = "b" * 32 if fault == "nonce" else nonce
    received_token = "bad" if fault == "malformed" else token
    status: dict[str, object] = {
        "exited": True,
        "exitcode": 1 if fault == "nonzero" else 0,
        "out-data": f"AGW_RUNTIME_1:{received_nonce}:{state}:{received_token}\n",
    }
    if fault == "signal":
        del status["exitcode"]
        status["signal"] = 15
    delivery.record_status(status)
    assert delivery.closure_proven is (fault is None)


def test_deadline_then_fresh_exact_closure_settles_same_attempt_without_replay(scenario) -> None:
    _, owner, operation, _, _, _, qga, deliveries = scenario
    outcome = _run(scenario)
    result = reduce_owned_inline_result(outcome)
    assert result.application_state is ApplicationState.UNKNOWN
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    assert attempt is not None and active.borrow.has_outstanding_attempt
    assert active.prepared is None and active.candidate is None
    assert active.outcome is not None and active.outcome.candidate is None
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert qga.calls == [("POST", "exec"), ("GET", "exec-status?pid=71")]
    operation.observe_inline_cleanup(Deadline.after(2))
    assert deliveries[0].closure_proven and active.operation.outstanding_attempt is None
    assert not active.borrow.has_outstanding_attempt
    assert not operation.active_inline_calls and not operation.unfinished_inline_executions
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations()
    operation.observe_inline_cleanup(Deadline.after(2))
    operation.finish()
    assert result.application_state is ApplicationState.UNKNOWN
    assert qga.calls == [("POST", "exec"), ("GET", "exec-status?pid=71"), ("GET", "exec-status?pid=71")]


@pytest.mark.parametrize(
    "fault", ["lost-ack", "get-failure", "wrong-nonce", "missing-ready", "missing-status", "nonzero", "signal"]
)
def test_unproved_or_lost_status_never_releases_and_failed_get_is_not_retried(scenario, fault: str) -> None:
    _, _, operation, _, _, _, qga, _ = scenario
    qga.fault = fault
    outcome = _run(scenario)
    assert reduce_owned_inline_result(outcome).application_state is ApplicationState.UNKNOWN
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    with pytest.raises(StateError):
        operation.observe_inline_cleanup(Deadline.after(2))
    calls = list(qga.calls)
    with pytest.raises(StateError):
        operation.observe_inline_cleanup(Deadline.after(2))
    assert qga.calls == calls and active.operation.outstanding_attempt is attempt
    assert operation.active_inline_calls == (active,)


@pytest.mark.parametrize("budget", [None, 0])
def test_invalid_cleanup_budget_does_not_touch_original_attempt_or_provider(scenario, budget) -> None:
    _, _, operation, _, _, _, qga, _ = scenario
    _run(scenario)
    calls = list(qga.calls)
    with pytest.raises(ValidationError):
        operation.observe_inline_cleanup(Deadline.after(budget))
    assert qga.calls == calls and operation.active_inline_calls


@pytest.mark.parametrize("boundary", ["ack", "terminal", "callback"])
def test_control_after_publication_retains_exact_original_and_uses_terminal_proof_without_get(
    scenario, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    _, _, operation, _, _, _, qga, deliveries = scenario
    original_cause = ValueError("original-cause")
    control = KeyboardInterrupt("original-control")
    control.__cause__ = original_cause
    method = "acknowledge" if boundary == "ack" else "record_status"
    original = getattr(ProxmoxHelperDelivery, method)

    def publish_then_interrupt(self, value):
        original(self, value)
        raise control

    if boundary == "callback":

        def interrupt_output(*_args):
            raise control

        monkeypatch.setattr(InlineObserver, "accept", interrupt_output)
    else:
        monkeypatch.setattr(ProxmoxHelperDelivery, method, publish_then_interrupt)
    qga.running = False
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(scenario)
    assert caught.value is control and isinstance(control.__cause__, InlineExecutionControlFact)
    assert control.__cause__.__cause__ is original_cause
    (active,) = operation.active_inline_calls
    assert active.prepared is None and active.outcome is not None and active.outcome.candidate is None
    calls = list(qga.calls)
    monkeypatch.setattr(ProxmoxHelperDelivery, method, original)
    operation.observe_inline_cleanup(Deadline.after(2))
    assert deliveries[0].closure_proven
    assert len(qga.calls) == len(calls) + (boundary == "ack")
    operation.finish()


def test_lost_settlement_reply_retries_same_proof_without_status_replay(
    scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, operation, _, _, _, qga, _ = scenario
    _run(scenario)
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    original = OperationAttempt.settle
    control = KeyboardInterrupt("settlement reply lost")

    def settle_then_interrupt(self):
        original(self)
        raise control

    monkeypatch.setattr(OperationAttempt, "settle", settle_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        operation.observe_inline_cleanup(Deadline.after(2))
    assert caught.value is control and active.operation.outstanding_attempt is attempt
    assert active.operation.coordination_uncertain and active.operation.pending_remote_effects
    calls = list(qga.calls)
    monkeypatch.setattr(OperationAttempt, "settle", original)
    operation.retry_inline_bookkeeping()
    assert qga.calls == calls and not operation.active_inline_calls
    operation.finish()


def test_lost_database_resolution_reply_retries_finish_without_native_observation(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, operation, _, _, _, qga, _ = scenario
    outcome = _run(scenario)
    operation.observe_inline_cleanup(Deadline.after(2))
    calls = list(qga.calls)
    original = type(database.operations).resolve_lifecycle_obligation
    cause = ValueError("original cause")
    control = KeyboardInterrupt("database resolution reply lost")
    control.__cause__ = cause

    def commit_then_interrupt(self, ownership, obligation_id):
        original(self, ownership, obligation_id)
        raise control

    monkeypatch.setattr(type(database.operations), "resolve_lifecycle_obligation", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        operation.finish()
    assert caught.value is control and control.__cause__ is cause
    assert not operation.active_inline_calls and not owner.list_pending_lifecycle_obligations()
    monkeypatch.setattr(type(database.operations), "resolve_lifecycle_obligation", original)
    operation.finish()
    assert qga.calls == calls and reduce_owned_inline_result(outcome).application_state is ApplicationState.UNKNOWN


@pytest.mark.parametrize("fence", ["route", "local", "stale", "stale-after-route", "binding"])
def test_fences_refuse_before_exact_pid_observation(scenario, monkeypatch: pytest.MonkeyPatch, fence: str) -> None:
    database, owner, operation, _, _, _, qga, _ = scenario
    _run(scenario)
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    assert attempt is not None
    if fence == "route":

        def refuse(_deadline):
            raise StateError("known route changed")

        operation._route_check = refuse
    elif fence == "local":
        monkeypatch.setattr(attempt.local_delivery, "close", lambda _deadline: False)
    elif fence == "binding":
        assert operation._native_binding is not None
        connection = ProxmoxConnection("https://other.test", "node", 101, "token", "secret")
        operation._native_binding = replace(operation._native_binding, carrier=ProxmoxCarrier(connection))
    elif fence == "stale-after-route":

        def take_over(_deadline):
            OperationOwner.recover(database.operations, owner.ownership, "b" * 32)

        operation._route_check = take_over
    else:
        OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
    calls = list(qga.calls)
    with pytest.raises(StateError):
        operation.observe_inline_cleanup(Deadline.after(2))
    assert qga.calls == calls and active.operation.outstanding_attempt is attempt
