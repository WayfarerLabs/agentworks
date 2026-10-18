"""Exact recovery bookkeeping interruptions never replay a helper."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.db.operations import OperationRepository
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._prepared_helper_obligation import (
    decode_prepared_helper_obligation,
    encode_prepared_helper_obligation,
)
from agentworks.execution._prepared_stage_recovery import PreparedHelperStageRecovery
from agentworks.execution.carrier import Deadline, Dispatch, Failure
from agentworks.operations import (
    LifecycleObligation,
    OperationOwner,
    RecoveredLifecycleObligation,
    RecoveryAttempt,
    RecoveryDispatch,
)
from tests.execution.test_prepared_stage_recovery import setup as setup

pytestmark = pytest.mark.windows


@pytest.mark.parametrize("phase", ["open", "begin", "settle", "close"])
@pytest.mark.parametrize("action", ["reconcile", "cleanup"])
def test_lost_local_transition_keeps_actual_custody_without_replay(setup, monkeypatch, phase, action):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    if action == "cleanup":
        with span.action(Deadline.after(2)) as context:
            recovery.reconcile(context, Deadline.after(1))
    before = len(helpers.requests)
    cls, method = {
        "open": (RecoveredLifecycleObligation, "open_dispatch"),
        "begin": (RecoveryDispatch, "begin_attempt"),
        "settle": (RecoveryAttempt, "settle"),
        "close": (RecoveryDispatch, "close"),
    }[phase]
    original = getattr(cls, method)

    def interrupted(resource):
        original(resource)
        raise KeyboardInterrupt

    monkeypatch.setattr(cls, method, interrupted)
    with pytest.raises(KeyboardInterrupt), span.action(Deadline.after(2)) as context:
        getattr(recovery, action)(context, Deadline.after(1))
    assert len(helpers.requests) == before + (phase in {"settle", "close"})
    monkeypatch.setattr(cls, method, original)
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and facts.helper_termination_known and not facts.dispatch_retained
    assert recovery.dispatch is None and recovery.attempt is None
    current = owner.inspect_lifecycle_obligation(row.obligation_id)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert current.payload_version == (2 if action == "cleanup" and phase in {"settle", "close"} else 1)
    if action == "cleanup" and phase in {"settle", "close"}:
        assert recovery.cleaned and recovery.resolution_pending
        with span.action(Deadline.after(2)) as context:
            recovery.retry_resolution(context, Deadline.after(1))
        assert owner.inspect_lifecycle_obligation(row.obligation_id).state is LifecycleObligationState.RESOLVED
    assert len(helpers.requests) == before + (phase in {"settle", "close"})


@pytest.mark.parametrize("fault", ["execute", "unknown", "nonzero"])
def test_local_drain_cannot_invent_remote_termination_or_allow_another_helper(setup, fault):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    if fault == "execute":
        helpers.error = KeyboardInterrupt()
        helpers.unsettled = True
        with pytest.raises(KeyboardInterrupt), span.action(Deadline.after(2)) as context:
            recovery.reconcile(context, Deadline.after(1))
    else:
        helpers.dispatch = Dispatch.UNKNOWN if fault == "unknown" else Dispatch.SENT
        helpers.code = 1
        with span.action(Deadline.after(2)) as context:
            recovery.reconcile(context, Deadline.after(1))
    assert recovery.attempt.local_delivery is helpers.custody
    assert recovery.dispatch is not None
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and not facts.helper_termination_known and facts.dispatch_retained
    with pytest.raises(StateError):
        PreparedHelperStageRecovery.open(owner, row)
    with pytest.raises(StateError), span.action(Deadline.after(1)):
        pass
    assert owner.inspect_lifecycle_obligation(row.obligation_id).payload_version == 2
    assert len(helpers.requests) == 1


def test_positive_remote_completion_waits_for_real_local_delivery(setup, monkeypatch):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    helpers.unsettled = True
    original = LocalDeliveryCustody.close
    monkeypatch.setattr(LocalDeliveryCustody, "close", lambda self, deadline: False)
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    assert recovery.attempt.local_delivery is helpers.custody
    facts = recovery.drain(Deadline.after(1))
    assert not facts.local_settled and facts.helper_termination_known and facts.dispatch_retained
    monkeypatch.setattr(LocalDeliveryCustody, "close", original)
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and facts.helper_termination_known and not facts.dispatch_retained
    current = owner.inspect_lifecycle_obligation(row.obligation_id)
    assert current.payload == row.payload and current.payload_version == 1 and current.payload_revision == 2
    assert len(helpers.requests) == 1


@pytest.mark.parametrize("committed", [False, True])
def test_lost_resolution_retries_only_own_row_bookkeeping_under_fresh_action(setup, monkeypatch, committed):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    original = OperationRepository.resolve_lifecycle_obligation
    resolutions = []

    def lost(repository, ownership, obligation_id):
        resolutions.append(obligation_id)
        if committed:
            original(repository, ownership, obligation_id)
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", lost)
    with pytest.raises(KeyboardInterrupt), span.action(Deadline.after(2)) as context:
        recovery.cleanup(context, Deadline.after(1))
    assert recovery.cleaned and recovery.resolution_pending
    assert len(helpers.requests) == 2
    assert owner.inspect_lifecycle_obligation(row.obligation_id).payload_version == 2
    with pytest.raises(StateError):
        recovery.retry_resolution(context, Deadline.after(1))
    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", original)
    with span.action(Deadline.after(2)) as context:
        with pytest.raises(StateError):
            recovery.cleanup(context, Deadline.after(1))
        recovery.retry_resolution(context, Deadline.after(1))
    assert recovery.cleaned and not recovery.resolution_pending
    assert resolutions == [row.obligation_id] and len(helpers.requests) == 2
    assert owner.inspect_lifecycle_obligation(row.obligation_id).state is LifecycleObligationState.RESOLVED
    assert owner.list_pending_lifecycle_obligations() != ()


@pytest.mark.parametrize("fault", ["unknown", "nonzero", "corrupt", "incomplete", "not_sent", "runtime", "failure"])
def test_cleanup_requires_all_positive_cleaned_and_closure_facts(setup, fault):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    if fault == "unknown":
        helpers.dispatch = Dispatch.UNKNOWN
    elif fault == "nonzero":
        helpers.code = 1
    elif fault == "corrupt":
        helpers.corrupt = True
    elif fault == "not_sent":
        helpers.dispatch = Dispatch.NOT_SENT
    elif fault == "runtime":
        helpers.runtime = "missing"
    elif fault == "failure":
        helpers.failure = Failure.OBSERVATION
    else:
        helpers.complete = False
    with span.action(Deadline.after(2)) as context:
        recovery.cleanup(context, Deadline.after(1))
        with pytest.raises(StateError):
            recovery.retry_resolution(context, Deadline.after(1))
    assert not recovery.cleaned and not recovery.resolution_pending
    current = owner.inspect_lifecycle_obligation(row.obligation_id)
    assert current.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert current.payload_version == (2 if fault in {"unknown", "nonzero"} else 1)


def test_current_row_revalidation_and_replacement_adapter_cannot_bypass_custody(setup):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    replacement = PreparedHelperStageRecovery.open(owner, row)
    activate()
    with span.action(Deadline.after(2)) as context:
        replacement.reconcile(context, Deadline.after(1))
    assert recovery.cleanup_debt is None and replacement.cleanup_debt is not None
    assert len(helpers.requests) == 1
    with span.action(Deadline.after(2)) as context, pytest.raises(StateError):
        recovery.cleanup(context, Deadline.after(1))
    rebound = owner.rebind_lifecycle_obligation(
        row.obligation_id, row.obligation_kind, payload_version=1, payload=row.payload
    )
    changed = replace(decode_prepared_helper_obligation(row.payload), token=b"f" * 16)
    rebound.publish_payload(
        expected_revision=rebound._persisted_obligation.payload_revision,
        payload_version=1,
        payload=encode_prepared_helper_obligation(changed),
    )
    with span.action(Deadline.after(2)) as context, pytest.raises(StateError):
        replacement.cleanup(context, Deadline.after(1))
    assert len(helpers.requests) == 1
    with pytest.raises(StateError):
        replacement.drain(Deadline.after(1))


@pytest.mark.parametrize("fault", ["expired", "budget", "guest"])
def test_resolution_retry_revalidates_exact_current_action_before_database_write(setup, monkeypatch, fault):
    _, _, _, span, recovery, helpers, activate, _ = setup
    activate()
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    original = OperationRepository.resolve_lifecycle_obligation

    def lost(repository, ownership, obligation_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", lost)
    with pytest.raises(KeyboardInterrupt), span.action(Deadline.after(2)) as context:
        recovery.cleanup(context, Deadline.after(1))
    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", original)
    deadline = Deadline.after(1)
    with span.action(deadline) as context:
        if fault == "expired":
            object.__setattr__(deadline, "expires_at", 0.0)
        elif fault == "budget":
            deadline = Deadline.after(10)
        else:
            context = replace(context, guest=replace(context.guest, init_start_ticks=1))
        with pytest.raises((StateError, ValidationError)):
            recovery.retry_resolution(context, deadline)
    assert recovery.resolution_pending and len(helpers.requests) == 2


def test_positive_cleanup_with_unsettled_local_delivery_retains_fact_until_drain(setup, monkeypatch):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    helpers.unsettled = True
    original = LocalDeliveryCustody.close
    monkeypatch.setattr(LocalDeliveryCustody, "close", lambda self, deadline: False)
    with span.action(Deadline.after(2)) as context:
        recovery.cleanup(context, Deadline.after(1))
    assert recovery._cleanup_confirmed and not recovery.cleaned
    assert recovery.attempt.local_delivery is helpers.custody
    assert owner.inspect_lifecycle_obligation(row.obligation_id).payload_version == 2
    monkeypatch.setattr(LocalDeliveryCustody, "close", original)
    assert not recovery.drain(Deadline.after(1)).dispatch_retained
    assert recovery.cleaned and recovery.resolution_pending
    with span.action(Deadline.after(2)) as context:
        with pytest.raises(StateError):
            recovery.cleanup(context, Deadline.after(1))
        recovery.retry_resolution(context, Deadline.after(1))
    assert len(helpers.requests) == 2


def test_another_genuine_span_cannot_replace_retained_span(setup, monkeypatch):
    from agentworks.capabilities.base import RunContext
    from agentworks.execution.carriers.wsl2 import WSL2Carrier
    from agentworks.vms._recovery_vm_span import RecoveryVMSpan
    from tests.execution.test_recovery_guest_preparation import FixedCarrier
    from tests.execution.test_wsl2_platform_hold import FakeNative, FakeObserver

    database, owner, _, span, recovery, helpers, activate, _ = setup
    activate()
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    fixed = FixedCarrier()

    def prepare(carrier, invocation, *, io, deadline, custody):
        return fixed.execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(WSL2Carrier, "execute", prepare)
    other = RecoveryVMSpan(
        database,
        "box",
        span._platform,
        RunContext(),
        owner=owner,
        hold_id="e" * 32,
        preparation_id="f" * 32,
        workload_account="admin",
        native=FakeNative([]),
        observer=FakeObserver([]),
    )
    other.open(Deadline.after(10))
    with other.action(Deadline.after(2)) as context, pytest.raises(StateError):
        recovery.reconcile(context, Deadline.after(1))
    assert len(helpers.requests) == 1 and recovery.dispatch is None


@pytest.mark.parametrize("phase", ["marker", "restore"])
@pytest.mark.parametrize("committed", [False, True])
def test_lost_publication_reconciles_only_exact_retained_cas_and_never_replays_helper(
    setup, monkeypatch, phase, committed
):
    _, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    original = LifecycleObligation.publish_payload
    publications = []

    def lost(obligation, **options):
        if options["payload_version"] == (2 if phase == "marker" else 1):
            publications.append(options)
            if committed:
                original(obligation, **options)
            raise KeyboardInterrupt
        return original(obligation, **options)

    monkeypatch.setattr(LifecycleObligation, "publish_payload", lost)
    with pytest.raises(KeyboardInterrupt), span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    assert len(helpers.requests) == (phase == "restore")
    assert recovery._publication is not None and len(publications) == 1
    monkeypatch.setattr(LifecycleObligation, "publish_payload", original)
    facts = recovery.drain(Deadline.after(1))
    assert facts.local_settled and facts.helper_termination_known and not facts.dispatch_retained
    current = owner.inspect_lifecycle_obligation(row.obligation_id)
    assert current.payload_version == 1 and current.payload == row.payload and current.payload_revision == 2
    assert recovery._publication is None
    assert len(helpers.requests) == (phase == "restore")


@pytest.mark.parametrize("unknown", [False, True])
def test_next_takeover_after_support_disposal_refuses_inflight_marker_but_accepts_restored_idle_row(setup, unknown):
    database, owner, row, span, recovery, helpers, activate, _ = setup
    activate()
    helpers.dispatch = Dispatch.UNKNOWN if unknown else Dispatch.SENT
    with span.action(Deadline.after(2)) as context:
        recovery.reconcile(context, Deadline.after(1))
    successor = OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
    # Model separate support-adapter disposition in SQLite; this supplies no
    # helper-termination proof and must never clear the stage's own marker.
    for support in successor.list_pending_lifecycle_obligations():
        if support.obligation_id != row.obligation_id:
            LifecycleObligation(successor, support).resolve()
    current = successor.inspect_lifecycle_obligation(row.obligation_id)
    assert current is not None
    assert successor.list_pending_lifecycle_obligations() == (current,)
    if unknown:
        assert current.payload_version == 2 and current.payload_revision == 1
        with pytest.raises(StateError):
            PreparedHelperStageRecovery.open(successor, current)
    else:
        assert current.payload_version == 1 and current.payload_revision == 2
        reopened = PreparedHelperStageRecovery.open(successor, current)
        assert reopened.cleanup_debt is None and reopened._span is None
    assert len(helpers.requests) == 1
