"""Retained managed reads settle only their original acknowledged QGA helper."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as core
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_disposal_bundle import FIXED_BUNDLE as DISPOSAL_BUNDLE
from agentworks.execution._managed_disposal_protocol import decode_request as decode_disposal
from agentworks.execution._managed_job_store import Stream
from agentworks.execution._managed_observation_bundle import FIXED_BUNDLE as OBSERVATION_BUNDLE
from agentworks.execution._managed_observation_exchange import (
    ManagedObservationCandidate,
    ManagedObservationState,
    PreparedManagedRead,
    _Collector,
    execute_managed_read,
    prepare_managed_read,
)
from agentworks.execution._managed_observation_protocol import ManagedOperation
from agentworks.execution._managed_observation_protocol import decode_request as decode_observation
from agentworks.execution._managed_observe_access import ManagedObserveControlFact, ManagedObserveOutcome
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
)
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.carriers._proxmox_helper_delivery import ProxmoxHelperDelivery
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, _ProxmoxWire
from agentworks.execution.models import JobRef
from agentworks.operations import OperationAttempt, OperationBorrow, OperationOwner

from . import test_independent_execution_reads as reads
from . import test_managed_disposal as disposal
from . import test_proxmox_inline_closure as inline
from .test_managed_job_access import RUN
from .test_managed_observation import GUEST, _launch
from .test_managed_start_operation import GUEST as RESOURCE_GUEST

inline_scenario = inline.scenario
resource_observer = reads.observer
pytestmark = inline.pytestmark


@pytest.fixture
def scenario(inline_scenario):
    database, owner, operation, base, _, runtime, qga, deliveries = inline_scenario
    # DIRECT does not elevate the local test process. The actual root helper
    # refuses identity here while still emitting authentic runtime evidence.
    plan = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
    operation._target = replace(operation._target, boot_id=vm_guest_boot_id(GUEST))
    return database, owner, operation, base, plan, runtime, qga, deliveries


def _read(
    scenario: inline.Scenario,
    stream: Stream | None = None,
    *,
    runtime: RuntimeSelection | None = None,
    deadline: Deadline | None = None,
) -> tuple[ManagedObservationCandidate, ManagedObserveOutcome]:
    _, _, operation, base, plan, selection, qga, _ = scenario
    operation._bootstrap = _NumericGuestBootstrap(plan, GUEST)
    return operation.observe_managed(
        base,
        expected_launch=_launch(),
        root_plan=plan,
        runtime_selection=runtime or selection,
        deadline=deadline or qga.deadline,
        guest=GUEST,
        stream=stream,
    )


def _detached(active: core._ActiveHelperCall, prepared: PreparedManagedRead | None = None) -> None:
    assert not active.inline and active.prepared is None
    assert active.candidate is None or active.candidate.observation is None
    assert active.outcome is None or active.outcome.candidate is None
    if prepared is not None:
        assert prepared.io is None and prepared.invocation is None
        assert prepared._collector is None and prepared._reader is None
        assert prepared._runtime is None and prepared._stderr is None


@pytest.mark.parametrize("stream", [None, Stream.STDOUT, Stream.STDERR])
def test_deadline_closure_settles_original_read_without_application_upgrade(scenario, monkeypatch, stream):
    _, owner, operation, _, _, _, qga, deliveries = scenario
    prepared_reads = []
    execute = execute_managed_read

    def capture(carrier, prepared, *, deadline):
        prepared_reads.append(prepared)
        assert operation.active_inline_calls[0].closure_expectation is prepared.closure_expectation
        assert not qga.calls
        return execute(carrier, prepared, deadline=deadline)

    monkeypatch.setattr(core, "execute_managed_read", capture)
    candidate, outcome = _read(scenario, stream)
    assert candidate.dispatch is Dispatch.SENT and outcome.requires_owner_retention
    assert not outcome.terminal_proved
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    assert attempt is not None and owner._outstanding_attempt is attempt
    assert active.borrow.has_outstanding_attempt
    _detached(active, prepared_reads[0])
    assert deliveries[0]._expectation.guest == GUEST
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    operation.observe_inline_cleanup(Deadline.after(2))
    assert deliveries[0].closure_proven and active.operation.outstanding_attempt is None
    assert not active.borrow.has_outstanding_attempt and not operation.active_inline_calls
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations()
    assert candidate.carrier_completion is None and candidate.observation is None
    assert outcome.requires_owner_retention and not outcome.terminal_proved
    assert qga.calls == [("POST", "exec"), ("GET", "exec-status?pid=71"), ("GET", "exec-status?pid=71")]


@pytest.mark.parametrize("stream", [None, Stream.STDOUT])
@pytest.mark.parametrize("refusal", ["guest", "missing", "unusable"])
def test_normal_zero_refusal_closes_helper_without_terminal_proof(scenario, tmp_path: Path, stream, refusal):
    _, owner, operation, _, _, _, qga, deliveries = scenario
    qga.running = False
    runtime = None
    if refusal != "guest":
        path = tmp_path / "runtime"
        if refusal == "unusable":
            path.write_bytes(b"not executable")
            path.chmod(0o600)
        runtime = RuntimeSelection(RuntimeTargetOS.LINUX, str(path))
    candidate, outcome = _read(scenario, stream, runtime=runtime)
    if refusal == "guest":
        assert candidate.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        assert candidate.observation is not None
        assert candidate.observation.state is ManagedObservationState.REFUSED
    else:
        assert candidate.runtime_prerequisite.state is (
            RuntimePrerequisiteState.MISSING if refusal == "missing" else RuntimePrerequisiteState.UNUSABLE
        )
        assert candidate.observation is None
    assert deliveries[0].closure_proven and not outcome.requires_owner_retention and not outcome.terminal_proved
    assert not operation.active_inline_calls
    calls = list(qga.calls)
    operation.observe_inline_cleanup(Deadline.after(0))
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations() and qga.calls == calls


@pytest.mark.parametrize("stream", [None, Stream.STDOUT])
@pytest.mark.parametrize("boundary", ["ack", "terminal", "callback", "handoff-before", "handoff-after"])
def test_controls_keep_managed_fact_original_cause_and_detached_payload(scenario, monkeypatch, stream, boundary):
    _, _, operation, _, _, _, qga, deliveries = scenario
    qga.running = False
    cause = ValueError("original cause")
    control = KeyboardInterrupt("original control")
    control.__cause__ = cause
    prepared_reads = []
    execute = execute_managed_read

    def capture(carrier, prepared, *, deadline):
        prepared_reads.append(prepared)
        return execute(carrier, prepared, deadline=deadline)

    monkeypatch.setattr(core, "execute_managed_read", capture)
    target, method = (
        (ProxmoxHelperDelivery, "acknowledge")
        if boundary == "ack"
        else (ProxmoxHelperDelivery, "record_status")
        if boundary == "terminal"
        else (_Collector, "accept")
        if boundary == "callback"
        else (OperationBorrow, "handoff_retained_effect")
    )
    original = getattr(target, method)

    def interrupt(self, *args, **kwargs):
        if boundary in {"ack", "terminal", "handoff-after"}:
            original(self, *args, **kwargs)
        raise control

    monkeypatch.setattr(target, method, interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _read(scenario, stream)
    assert caught.value is control and isinstance(control.__cause__, ManagedObserveControlFact)
    assert control.__cause__.__cause__ is cause
    assert control.__cause__.outcome.candidate is None and not control.__cause__.outcome.terminal_proved
    (active,) = operation.active_inline_calls
    _detached(active, prepared_reads[0])
    calls = list(qga.calls)
    monkeypatch.setattr(target, method, original)
    operation.observe_inline_cleanup(Deadline.after(2))
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert deliveries[0].closure_proven and not operation.active_inline_calls
    assert len(qga.calls) == len(calls) + (boundary == "ack")


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("not_sent", [False, True])
@pytest.mark.parametrize("committed", [False, True])
def test_settle_loss_preserves_nonpayload_evidence_and_reconciles_original_attempt(
    scenario, monkeypatch, tracked, not_sent, committed
):
    _, owner, operation, _, _, _, qga, _ = scenario
    qga.running = False
    if not tracked:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=None)
    execute = ProxmoxCarrier._execute
    settle = OperationAttempt.settle
    attempts = []
    control = KeyboardInterrupt("settlement reply lost")
    cause = ValueError("original cause")
    control.__cause__ = cause

    def expire_then_execute(self, *args, **kwargs):
        qga.clock[0] = qga.deadline.expires_at + 1
        report = execute(self, *args, **kwargs)
        assert report.dispatch is Dispatch.NOT_SENT
        return report

    def interrupt(self):
        attempts.append(self)
        if committed:
            settle(self)
        raise control

    if not_sent:
        monkeypatch.setattr(ProxmoxCarrier, "_execute", expire_then_execute)
    monkeypatch.setattr(OperationAttempt, "settle", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _read(scenario)
    assert caught.value is control and isinstance(control.__cause__, ManagedObserveControlFact)
    assert control.__cause__.__cause__ is cause
    (active,) = operation.active_inline_calls
    (attempt,) = attempts
    assert active.operation.outstanding_attempt is attempt
    _detached(active)
    assert active.not_sent is (tracked and not_sent)
    calls = list(qga.calls)
    monkeypatch.setattr(OperationAttempt, "settle", settle)
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations() and not operation.active_inline_calls
    assert qga.calls == calls


@pytest.mark.parametrize(
    "fault", ["lost-ack", "get-failure", "wrong-nonce", "missing-ready", "missing-status", "nonzero", "signal"]
)
@pytest.mark.parametrize("stream", [None, Stream.STDOUT])
def test_lost_or_unproved_status_never_settles_or_replays(scenario, fault, stream):
    _, _, operation, _, _, _, qga, _ = scenario
    qga.fault = fault
    _, outcome = _read(scenario, stream)
    assert outcome.requires_owner_retention and not outcome.terminal_proved
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    _detached(active)
    with pytest.raises(StateError):
        operation.observe_inline_cleanup(Deadline.after(2))
    calls = list(qga.calls)
    with pytest.raises(StateError):
        operation.observe_inline_cleanup(Deadline.after(2))
    assert qga.calls == calls and active.operation.outstanding_attempt is attempt
    assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize("fence", ["binding", "guest", "stale", "local", "route", "stale-after-route"])
def test_cleanup_fences_precede_status_get(scenario, monkeypatch, fence):
    database, owner, operation, _, _, _, qga, _ = scenario
    _read(scenario)
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    if fence == "binding":
        operation._native_binding = replace(operation._native_binding, carrier=object())
    elif fence == "guest":
        operation._bootstrap = replace(operation._bootstrap, guest=replace(GUEST, instance_marker="e" * 32))
    elif fence == "stale":
        OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
    elif fence == "local":
        monkeypatch.setattr(attempt.local_delivery, "close", lambda _deadline: False)
    elif fence == "route":

        def refuse(_deadline):
            raise StateError("route changed")

        operation._route_check = refuse
    else:
        operation._route_check = lambda _deadline: OperationOwner.recover(
            database.operations, owner.ownership, "b" * 32
        )
    calls = list(qga.calls)
    with pytest.raises(StateError):
        operation.observe_inline_cleanup(Deadline.after(2))
    assert qga.calls == calls and active.operation.outstanding_attempt is attempt


def test_preparation_is_one_shot_and_expiry_never_enters_carrier(scenario):
    _, _, _, base, plan, runtime, qga, _ = scenario
    prepared = prepare_managed_read(
        operation=ManagedOperation.OBSERVE,
        expected_launch=_launch(),
        stream=None,
        plan=plan,
        deadline=Deadline.after(0),
        runtime_selection=runtime,
        guest=GUEST,
    )
    candidate = execute_managed_read(base, prepared, deadline=Deadline.after(2))
    assert candidate.dispatch is Dispatch.NOT_SENT and candidate.carrier_completion is None
    assert prepared.io is None and not qga.calls
    with pytest.raises(ValidationError):
        execute_managed_read(base, prepared, deadline=Deadline.after(2))


def test_invalid_preparation_refuses_before_borrow(scenario, monkeypatch):
    _, owner, _, _, _, _, qga, _ = scenario
    monkeypatch.setattr(owner, "borrow", lambda: pytest.fail("borrow after invalid preparation"))
    with pytest.raises(ValidationError):
        _read(scenario, runtime=RuntimeSelection(RuntimeTargetOS.DARWIN, "/missing"))
    assert not qga.calls


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("committed", [False, True])
def test_final_handoff_discards_facts_and_output_but_keeps_original_caller_candidate(
    scenario, monkeypatch, tracked, committed
):
    _, _, operation, _, _, _, qga, _ = scenario
    qga.running = False
    if not tracked:
        operation._native_binding = replace(operation._native_binding, _new_helper_delivery=None)
    main: Any = reads.ResourceDelivery()
    main.disposition = "complete-capture"
    main.content = b"application-output-secret"

    def request(_self, method, suffix, *, body=None, **kwargs):
        result = qga.request(method, suffix, body=body, **kwargs)
        if method == "POST":
            data = json.loads(body)["input-data"].encode("ascii")
            read = decode_observation(data[len(OBSERVATION_BUNDLE.prefix) :])
            qga.terminal["out-data"] = f"AGW_RUNTIME_1:{read.nonce}:ready:0\n" + main.observe_response(read).decode(
                "ascii"
            )
        return result

    monkeypatch.setattr(_ProxmoxWire, "request", request)
    candidates = []

    def capture(carrier, prepared, *, deadline):
        candidate = execute_managed_read(carrier, prepared, deadline=deadline)
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(core, "execute_managed_read", capture)
    control = KeyboardInterrupt("handoff interrupted")
    cause = ValueError("original cause")
    control.__cause__ = cause
    handoff = OperationBorrow.handoff_retained_effect

    def interrupt(self):
        if committed:
            handoff(self)
        raise control

    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _read(scenario, Stream.STDOUT)
    assert caught.value is control and isinstance(control.__cause__, ManagedObserveControlFact)
    assert control.__cause__.__cause__ is cause
    (active,) = operation.active_inline_calls
    _detached(active)
    observation = candidates[0].observation
    assert observation is not None and observation.output == main.content and observation.facts
    assert active.borrow._closed is committed
    calls = list(qga.calls)
    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", handoff)
    operation.retry_inline_bookkeeping()
    operation.finish()
    assert qga.calls == calls and not operation.active_inline_calls
    assert observation.output == main.content


@pytest.mark.parametrize("not_sent", [False, True])
def test_stale_owner_never_reconciles_lost_settlement_even_with_positive_evidence(scenario, monkeypatch, not_sent):
    database, owner, operation, _, _, _, qga, _ = scenario
    qga.running = False
    execute = ProxmoxCarrier._execute

    def expire(self, *args, **kwargs):
        qga.clock[0] = qga.deadline.expires_at + 1
        return execute(self, *args, **kwargs)

    if not_sent:
        monkeypatch.setattr(ProxmoxCarrier, "_execute", expire)
    settle = OperationAttempt.settle

    def interrupt(_self):
        raise KeyboardInterrupt("lost settlement")

    monkeypatch.setattr(OperationAttempt, "settle", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _read(scenario)
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    OperationOwner.recover(database.operations, owner.ownership, "b" * 32)
    monkeypatch.setattr(OperationAttempt, "settle", settle)
    calls = list(qga.calls)
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert qga.calls == calls and active.operation.outstanding_attempt is attempt


def test_proven_terminal_callback_control_settles_local_pending_before_no_io_bookkeeping(scenario, monkeypatch):
    _, _, operation, _, _, _, qga, _ = scenario
    qga.running = False

    def interrupt(*_args):
        raise KeyboardInterrupt("callback stopped")

    monkeypatch.setattr(_Collector, "accept", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _read(scenario)
    (active,) = operation.active_inline_calls
    attempt = active.operation.outstanding_attempt
    assert attempt is not None and active.closure_delivery.closure_proven
    attempt.local_delivery.begin_process()
    calls = list(qga.calls)
    operation._route_check = lambda _deadline: pytest.fail("fresh route observation")
    with pytest.raises(ValidationError):
        operation.observe_inline_cleanup(Deadline.after(0))
    assert not attempt.local_delivery.settled
    operation.observe_inline_cleanup(Deadline.after(2))
    operation.finish()
    assert attempt.local_delivery.settled and qga.calls == calls and not operation.active_inline_calls


def test_resource_prerequisite_cleanup_never_disposes_then_fresh_terminal_read_authorizes_action(
    scenario, resource_observer, monkeypatch
):
    _, _, _, base, plan, runtime, qga, deliveries = scenario
    database, repository, operation, _, main, _ = resource_observer
    binding = replace(operation._native_binding, carrier=base, runtime_selection=runtime)

    def factory(expectation):
        delivery = ProxmoxHelperDelivery(base._wire._connection, expectation)
        deliveries.append(delivery)
        return delivery

    operation._native_binding = replace(binding, _new_helper_delivery=factory)
    operation._bootstrap = _NumericGuestBootstrap(plan, RESOURCE_GUEST)
    before = repository.inspect(RUN)
    actions = []
    observe = operation.observe_job
    outcomes = []

    def capture(*args):
        outcome = observe(*args)
        outcomes.append(outcome)
        return outcome

    operation.observe_job = capture

    def request(_self, method, suffix, *, body=None, **kwargs):
        result = qga.request(method, suffix, body=body, **kwargs)
        if method == "POST":
            data = json.loads(body)["input-data"].encode("ascii")
            if data.startswith(OBSERVATION_BUNDLE.prefix):
                read_request = decode_observation(data[len(OBSERVATION_BUNDLE.prefix) :])
                actions.append("observe")
                response = main.observe_response(read_request)
                nonce = read_request.nonce
            else:
                assert data.startswith(DISPOSAL_BUNDLE.prefix)
                disposal_request = decode_disposal(data[len(DISPOSAL_BUNDLE.prefix) :])
                actions.append("dispose")
                response = disposal._disposed(disposal_request)
                nonce = disposal_request.nonce
            # Fake QGA supplies positive guest evidence only for this scenario.
            # The actual prepared source ran above and refused local identity.
            qga.terminal["out-data"] = f"AGW_RUNTIME_1:{nonce}:ready:0\n" + response.decode("ascii")
        return result

    monkeypatch.setattr(_ProxmoxWire, "request", request)
    reference = JobRef(RUN.run_id)
    original = operation.dispose_job(reference, base, qga.deadline)
    assert original.disposed is None and original.deadline_exceeded
    assert actions == ["observe"] and not outcomes[0].terminal_proved
    (active,) = operation.active_inline_calls
    assert active.disposal is None and operation.managed_runs == ()
    _detached(active)
    operation.observe_inline_cleanup(Deadline.after(2))
    assert deliveries[0].closure_proven and not operation.active_inline_calls
    assert actions == ["observe"] and repository.inspect(RUN) == before
    assert original.disposed is None and not outcomes[0].terminal_proved
    assert outcomes[0].candidate.carrier_completion is None
    assert not any(
        row.obligation_kind == "managed-dispose" for row in operation._owner.list_pending_lifecycle_obligations()
    )
    qga.deadline = Deadline.after(2)
    result = operation.dispose_job(reference, base, qga.deadline)
    assert result.disposed is True and actions == ["observe", "observe", "dispose"]
    assert outcomes[1].terminal_proved and original.disposed is None
    operation.finish()
    assert not operation.active_inline_calls and operation.managed_runs == ()
