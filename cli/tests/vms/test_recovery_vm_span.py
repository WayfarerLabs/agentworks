"""SQLite recovery composition with synthetic WSL2 native transitions.

Real fixed protocols and lifecycle admission run against fixtures. Windows
endpoint custody, privilege transitions and native queued-request drain are
not proved by these tests.
"""

from __future__ import annotations

from dataclasses import replace
from threading import Thread
from unittest.mock import Mock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, VMPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution._account import resolve_account
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._fixed_helper_operation import AttemptBoundHelperCarrier
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution._wsl2_lifecycle import HandleSettlement, HostClientStatus
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation, WSL2RouteRefusal
from agentworks.execution._wsl2_platform_hold import WSL2PlatformHold
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import Carrier, CarrierIO, Deadline, EndOfInput, PreparedInvocation, SinkOutput
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.operations import LifecycleObligation, OperationOwner, RecoveredLifecycleObligation, RecoveryDispatch
from agentworks.vms._recovery_vm_span import RecoveryVMSpan, RecoveryVMSpanControlFact
from tests.execution import test_recovery_guest_preparation as preparation_fixtures
from tests.execution._bound_carrier_support import bind_carrier as bind_carrier
from tests.execution.test_recovery_guest_preparation import FixedCarrier
from tests.execution.test_wsl2_platform_hold import BOOT, FakeNative, FakeObserver
from tests.vms.test_target_preparation import _MARKER

pytestmark = pytest.mark.windows

_HOLD_ID = "c" * 32
_PREPARATION_ID = "d" * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = Database(tmp_path / "state.db")
    database.insert_vm("box", "local", "box", admin_username="admin", instance_marker=_MARKER)
    database.update_vm_platform_metadata("box", {"distro_name": "Ubuntu"})
    predecessor = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "box"), "file-download"
    )
    old = predecessor.register_lifecycle_obligation("file-call", payload_version=1, payload=b"old")
    old.mark_possible_effect()
    owner = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
    platform = WSL2Platform("local", {})
    monkeypatch.setattr(platform, "observe_execution_power", Mock(return_value=VMStatus.RUNNING))
    monkeypatch.setattr(platform, "observe_provider_locator", Mock(return_value=ProviderLocator("wsl2:registration")))
    connection = WSL2Connection("Ubuntu", "admin", "wsl.exe")
    runtime = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
    monkeypatch.setattr(
        platform,
        "resolve_native_execution_binding",
        Mock(return_value=NativeExecutionBinding(WSL2Carrier(connection), "admin", runtime)),
    )
    carrier = FixedCarrier()
    routes = []

    def execute(selected, invocation, *, io, deadline, custody: LocalDeliveryCustody | None = None):
        routes.append(selected.connection)
        return carrier.execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(WSL2Carrier, "execute", execute)
    monkeypatch.setattr(preparation_fixtures, "_GUEST", VMGuestIdentity(_MARKER, BOOT, 4096))
    native, observer = FakeNative([]), FakeObserver([])
    span = RecoveryVMSpan(
        database,
        "box",
        platform,
        RunContext(),
        owner=owner,
        hold_id=_HOLD_ID,
        preparation_id=_PREPARATION_ID,
        workload_account="admin",
        native=native,
        observer=observer,
    )
    try:
        yield database, owner, old.obligation_id, platform, carrier, native, observer, span, routes
    finally:
        database.close()


def _resolve_old(database: Database, owner: OperationOwner, old_id: str) -> None:
    database.operations.resolve_lifecycle_obligation(owner.ownership, old_id)


def _validate(carrier: Carrier) -> None:
    carrier.validate(
        PreparedInvocation(("fixed",)),
        io=CarrierIO(input=EndOfInput(), output=SinkOutput(Mock(), Mock(), require_live=False)),
    )


def test_existing_recovery_claim_retains_hold_through_exact_bound_action(setup, monkeypatch):
    database, owner, old_id, _, carrier, native, _, span, routes = setup
    monkeypatch.setattr(OperationOwner, "acquire", Mock(side_effect=AssertionError("Second claim")))
    prepared = span.open(Deadline.after(10))
    assert carrier.calls == ["guest", "admin", "root"]
    assert [route.user for route in routes] == ["root", "admin", "admin"]
    assert prepared.guest == VMGuestIdentity(_MARKER, BOOT, 4096)
    assert prepared.ordinary_plan.expected == carrier.identities["admin"]
    assert prepared.root_plan.expected == carrier.identities["root"]
    with pytest.raises(StateError):
        _validate(prepared.carrier)
    action_deadline = Deadline.after(10)
    with span.action(action_deadline) as context:
        support = owner.admit_recovery_support_obligation(
            "carrier-dispatch", payload_version=1, payload=b"", obligation_id="e" * 32
        )
        dispatch = owner.rebind_possible_effect_lifecycle_obligation(
            support.obligation_id, "carrier-dispatch", payload_version=1, payload=b"", payload_revision=0
        ).open_dispatch()
        attempt = dispatch.begin_attempt()
        delivery = AttemptBoundHelperCarrier(context.carrier, attempt)
        result = resolve_account(delivery, "admin", action_deadline, context.runtime_selection)
        assert result.observation is not None
        assert result.observation.identity == context.ordinary_plan.expected
        attempt.settle()
        dispatch.close()
        support.resolve()
        escaped = context.carrier
        assert "eof" not in native.events
    with pytest.raises(StateError):
        _validate(escaped)
    with pytest.raises(StateError):
        span.close(Deadline.after(10))
    assert "eof" not in native.events
    _resolve_old(database, owner, old_id)
    span.close(Deadline.after(10))
    assert not span.requires_owner_retention
    assert all(row.state is LifecycleObligationState.RESOLVED for row in owner.list_lifecycle_obligations())
    assert database.operations.inspect(owner.ownership.scope).ownership == owner.ownership
    # Closing this span did not stop the aggregate owner's recovery admission.
    next_row = owner.admit_recovery_support_obligation("next", payload_version=1, payload=b"", obligation_id="f" * 32)
    next_row.resolve()
    with pytest.raises(StateError), span.action(Deadline.after(10)):
        pass
    span.close(Deadline.after(10))


@pytest.mark.parametrize(
    "fault", ["ordinary", "missing", "site", "marker", "account", "unknown", "raw", "late", "manual", "platform"]
)
def test_guards_refuse_before_support_activation(setup, fault):
    database, owner, old_id, platform, carrier, native, observer, span, _ = setup
    if fault == "ordinary":
        span._owner = OperationOwner(database.operations, owner.ownership)
    elif fault == "missing":
        span._vm_name = "absent"
    elif fault == "site":
        span._platform = WSL2Platform("other", {})
    elif fault == "marker":
        database._conn.execute("UPDATE vms SET instance_marker = NULL WHERE name = 'box'")
        database._conn.commit()
    elif fault == "account":
        span._workload_account = "worker"
    elif fault == "unknown":
        platform.observe_execution_power.return_value = VMStatus.UNKNOWN
    elif fault == "raw":
        platform.observe_execution_power.return_value = "running"
    elif fault == "manual":
        database.set_operator_stopped("box", True)
        platform.observe_execution_power.return_value = VMStatus.STOPPED
    elif fault == "platform":
        span._platform = Mock(spec=VMPlatform)
    deadline = Deadline.after(10)
    if fault == "late":

        def late(*args, **kwargs):
            object.__setattr__(deadline, "expires_at", 0.0)
            return VMStatus.RUNNING

        platform.observe_execution_power.side_effect = late
    with pytest.raises((StateError, ValidationError)):
        span.open(deadline)
    assert carrier.calls == []
    assert native.events == [] and observer.events == []
    assert {row.obligation_id for row in owner.list_lifecycle_obligations()} == {old_id}


def test_operator_intent_refresh_before_activation_and_running_manual_allowed(setup):
    database, _, _, platform, carrier, native, _, span, _ = setup
    count = 0

    def power(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            database.set_operator_stopped("box", True)
            return VMStatus.STOPPED
        return VMStatus.STOPPED

    platform.observe_execution_power.side_effect = power
    with pytest.raises(StateError):
        span.open(Deadline.after(10))
    assert native.events == [] and carrier.calls == []


@pytest.mark.parametrize("fault", ["prep_unknown", "prep_refusal", "boot", "ready_reply", "ready_payload"])
def test_start_and_preparation_failures_retain_actual_span(setup, monkeypatch, fault):
    database, owner, old_id, _, carrier, native, _, span, _ = setup
    if fault == "prep_unknown":
        carrier.abnormal = "admin"
    elif fault == "prep_refusal":
        carrier.refuse = "admin"
    elif fault == "boot":
        monkeypatch.setattr(
            preparation_fixtures, "_GUEST", VMGuestIdentity(_MARKER, "0" * 8 + "-0000-4000-8000-000000000001", 4096)
        )
    elif fault == "ready_reply":
        original = LifecycleObligation.publish_payload

        def lost(self, **kwargs):
            original(self, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(LifecycleObligation, "publish_payload", lost)
    else:
        monkeypatch.setattr(WSL2OwnedOperation, "_ready_is_durable", lambda *args: False)
    with pytest.raises((StateError, KeyboardInterrupt)) as caught:
        span.open(Deadline.after(10))
    assert isinstance(caught.value.__cause__, RecoveryVMSpanControlFact)
    assert caught.value.__cause__.span is span
    assert span.requires_owner_retention
    assert "dispatch" in native.events
    if fault.startswith("ready"):
        assert carrier.calls == []
    _resolve_old(database, owner, old_id)
    if fault == "prep_unknown":
        with pytest.raises(StateError):
            span.close(Deadline.after(10))
        assert "eof" not in native.events
        assert span.preparation is not None and span.preparation.requires_owner_retention
    elif fault in ("prep_refusal", "boot", "ready_payload"):
        span.close(Deadline.after(10))
        assert not span.requires_owner_retention


@pytest.mark.parametrize("fault", ["route", "marker", "metadata", "manual", "raw", "host", "takeover", "owner_stop"])
def test_action_revalidation_rejects_changed_or_closed_custody(setup, fault):
    database, owner, _, platform, carrier, native, _, span, _ = setup
    span.open(Deadline.after(10))
    if fault == "route":
        platform.observe_provider_locator.return_value = ProviderLocator("other")
    elif fault == "marker":
        database._conn.execute("UPDATE vms SET instance_marker = ? WHERE name = 'box'", ("f" * 32,))
        database._conn.commit()
    elif fault == "metadata":
        database.update_vm_platform_metadata("box", {"distro_name": "another"})
    elif fault == "manual":
        database.set_operator_stopped("box", True)
        platform.observe_execution_power.return_value = VMStatus.STOPPED
    elif fault == "raw":
        platform.observe_execution_power.return_value = "running"
    elif fault == "host":
        native.local = replace(native.local, host_client_status=HostClientStatus.EXITED)
    elif fault == "takeover":
        OperationOwner.recover(database.operations, owner.ownership, "f" * 32)
    else:
        owner.stop_admission()
    before = list(carrier.calls)
    with pytest.raises((StateError, WSL2RouteRefusal)), span.action(Deadline.after(10)):
        pass
    assert carrier.calls == before


@pytest.mark.parametrize("boundary", ["preparation", "action", "execute"])
@pytest.mark.parametrize("fault", ["exited", "unknown", "host_closed", "job_closed"])
def test_current_native_custody_refuses_without_new_guest_probes(setup, monkeypatch, boundary, fault, bind_carrier):
    _, _, _, _, carrier, native, _, span, _ = setup

    def change() -> None:
        if fault in ("exited", "unknown"):
            status = HostClientStatus.EXITED if fault == "exited" else HostClientStatus.UNKNOWN
            native.local = replace(native.local, host_client_status=status)
        elif fault == "host_closed":
            native.local = replace(native.local, host_handle_settlement=HandleSettlement.CLOSED)
        else:
            native.local = replace(native.local, job_handle_settlement=HandleSettlement.CLOSED)

    if boundary == "preparation":
        original = WSL2PlatformHold.start_recovery

        def changed(self, deadline, **kwargs):
            ready = original(self, deadline, **kwargs)
            change()
            return ready

        monkeypatch.setattr(WSL2PlatformHold, "start_recovery", changed)
        with pytest.raises(StateError):
            span.open(Deadline.after(10))
        assert carrier.calls == []
    else:
        span.open(Deadline.after(10))
        snapshots = native.snapshot_calls
        deadline = Deadline.after(10)
        if boundary == "action":
            change()
            with pytest.raises(StateError), span.action(deadline):
                pass
        else:
            with span.action(deadline) as context:
                change()
                with pytest.raises(StateError):
                    resolve_account(bind_carrier(context.carrier), "admin", deadline, context.runtime_selection)
        assert native.snapshot_calls > snapshots
        assert carrier.calls == ["guest", "admin", "root"]
    assert span.requires_owner_retention
    assert "eof" not in native.events


@pytest.mark.parametrize("boundary", ["preparation", "action", "execute"])
@pytest.mark.parametrize("fault", ["error", "control", "late"])
def test_current_native_snapshot_failure_retains_span_before_dispatch(
    setup, monkeypatch, boundary, fault, bind_carrier
):
    _, _, _, _, carrier, native, _, span, _ = setup
    deadline = Deadline.after(10)
    original = native.snapshot
    failure = KeyboardInterrupt() if fault == "control" else OSError("snapshot failed")

    def fail():
        if fault == "late":
            local = original()
            object.__setattr__(deadline, "expires_at", 0.0)
            return local
        raise failure

    def install() -> None:
        monkeypatch.setattr(native, "snapshot", fail)

    expected = TimeoutError if fault == "late" else type(failure)
    if boundary == "preparation":
        start = WSL2PlatformHold.start_recovery

        def started(self, deadline, **kwargs):
            ready = start(self, deadline, **kwargs)
            install()
            return ready

        monkeypatch.setattr(WSL2PlatformHold, "start_recovery", started)
        with pytest.raises(expected) as caught:
            span.open(deadline)
        assert carrier.calls == []
    else:
        span.open(deadline)
        if boundary == "action":
            install()
            with pytest.raises(expected) as caught, span.action(deadline):
                pass
        else:
            with pytest.raises(expected) as caught, span.action(deadline) as context:
                install()
                resolve_account(bind_carrier(context.carrier), "admin", deadline, context.runtime_selection)
        assert carrier.calls == ["guest", "admin", "root"]
    assert isinstance(caught.value.__cause__, RecoveryVMSpanControlFact)
    assert caught.value.__cause__.span is span
    assert span.requires_owner_retention and "eof" not in native.events
    if fault != "late":
        assert caught.value is failure


def test_route_changes_inside_action_refuse_before_actual_carrier(setup, bind_carrier):
    _, _, _, platform, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    deadline = Deadline.after(10)
    with span.action(deadline) as context:
        platform.observe_provider_locator.return_value = ProviderLocator("changed")
        with pytest.raises(WSL2RouteRefusal):
            resolve_account(bind_carrier(context.carrier), "admin", deadline, context.runtime_selection)
    assert carrier.calls == ["guest", "admin", "root"]


def test_close_wait_is_bounded_and_cannot_release_during_action(setup):
    database, owner, old_id, _, _, native, _, span, _ = setup
    span.open(Deadline.after(10))
    _resolve_old(database, owner, old_id)
    errors = []
    with span.action(Deadline.after(10)) as context:

        def close():
            try:
                span.close(Deadline.after(0.05))
            except TimeoutError as error:
                errors.append(error)

        worker = Thread(target=close)
        worker.start()
        worker.join(2)
        assert not worker.is_alive() and len(errors) == 1
        assert "eof" not in native.events
        _validate(context.carrier)
    with pytest.raises(StateError), span.action(Deadline.after(10)):
        pass
    span.close(Deadline.after(10))


def test_exact_hold_cleanup_retry_does_not_repeat_preparation(setup, monkeypatch):
    database, owner, old_id, _, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    _resolve_old(database, owner, old_id)
    original = WSL2PlatformHold.release

    def lost(self, deadline):
        original(self, deadline)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(WSL2PlatformHold, "release", lost)
        with pytest.raises(KeyboardInterrupt):
            span.close(Deadline.after(10))
    assert span.requires_owner_retention
    span.close(Deadline.after(10))
    assert not span.requires_owner_retention
    assert carrier.calls == ["guest", "admin", "root"]


@pytest.mark.parametrize("boundary", ["opened", "close-before", "close-after"])
def test_settled_preparation_cleanup_retry_precedes_hold_release(setup, monkeypatch, boundary):
    database, owner, old_id, _, carrier, native, _, span, _ = setup
    cls = RecoveredLifecycleObligation if boundary == "opened" else RecoveryDispatch
    method = "open_dispatch" if boundary == "opened" else "close"
    original = getattr(cls, method)

    def lost(self):
        if boundary != "close-before":
            original(self)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(cls, method, lost)
        with pytest.raises(KeyboardInterrupt):
            span.open(Deadline.after(10))
    with pytest.raises(StateError):
        span.close(Deadline.after(10))
    rows = {row.obligation_id: row for row in owner.list_lifecycle_obligations()}
    assert rows[_PREPARATION_ID].state is LifecycleObligationState.RESOLVED
    assert rows[old_id].state is rows[_HOLD_ID].state is LifecycleObligationState.POSSIBLE_EFFECT
    assert "eof" not in native.events
    _resolve_old(database, owner, old_id)
    span.close(Deadline.after(10))
    assert carrier.calls == ([] if boundary == "opened" else ["guest", "admin", "root"])
    assert "eof" in native.events
    assert not span.requires_owner_retention


@pytest.mark.parametrize("budget", [None, 30, 0, "foreign"])
def test_guarded_carrier_cannot_extend_or_outlive_action_budget(setup, budget, bind_carrier):
    _, _, _, _, carrier, _, _, span, _ = setup
    span.open(Deadline.after(10))
    action_deadline = Deadline.after(10)
    with span.action(action_deadline) as context:
        deadline = Mock(expires_at=action_deadline.expires_at) if budget == "foreign" else Deadline.after(budget)
        if budget == 0:
            object.__setattr__(action_deadline, "expires_at", 0.0)
        with pytest.raises((StateError, ValidationError)):
            resolve_account(bind_carrier(context.carrier), "admin", deadline, context.runtime_selection)
    assert carrier.calls == ["guest", "admin", "root"]


@pytest.mark.parametrize("method", ["open", "action", "close"])
def test_foreign_deadline_shape_is_refused(setup, method):
    _, _, _, _, carrier, native, _, span, _ = setup
    foreign = Mock(expires_at=Deadline.after(10).expires_at, expired=False)
    with pytest.raises(ValidationError):
        if method == "action":
            with span.action(foreign):
                pass
        else:
            getattr(span, method)(foreign)
    assert carrier.calls == [] and native.events == []


def test_running_vm_with_operator_stopped_intent_is_allowed(setup):
    database, _, _, _, carrier, _, _, span, _ = setup
    database.set_operator_stopped("box", True)
    span.open(Deadline.after(10))
    assert carrier.calls == ["guest", "admin", "root"]


def test_unknown_hold_cleanup_keeps_custody_until_exact_absence(setup):
    from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence

    database, owner, old_id, _, carrier, _, observer, span, _ = setup
    span.open(Deadline.after(10))
    _resolve_old(database, owner, old_id)
    observer.presence = GuestAnchorPresence.UNKNOWN
    with pytest.raises(StateError):
        span.close(Deadline.after(10))
    assert span.requires_owner_retention
    observer.presence = GuestAnchorPresence.ABSENT_CONFIRMED
    span.close(Deadline.after(10))
    assert not span.requires_owner_retention
    assert carrier.calls == ["guest", "admin", "root"]


def test_known_noncreation_cleanup_does_not_disposition_predecessor_debt(setup):
    _, owner, old_id, _, carrier, native, _, span, _ = setup
    native.fail_identity = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        span.open(Deadline.after(10))
    span.close(Deadline.after(10))
    assert not span.requires_owner_retention
    assert carrier.calls == []
    assert owner.list_lifecycle_obligations()[0].obligation_id == old_id
    assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT


def test_lost_support_registration_reply_retains_and_never_launches(setup, monkeypatch):
    _, owner, _, _, carrier, native, _, span, _ = setup
    original = OperationOwner.admit_recovery_support_obligation

    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(OperationOwner, "admit_recovery_support_obligation", lost)
    with pytest.raises(KeyboardInterrupt):
        span.open(Deadline.after(10))
    with pytest.raises(StateError):
        span.close(Deadline.after(10))
    assert span.requires_owner_retention
    assert carrier.calls == []
    assert "dispatch" not in native.events
    assert _HOLD_ID in {row.obligation_id for row in owner.list_lifecycle_obligations()}


def test_same_boot_ready_payload_from_another_anchor_is_refused_before_probes(setup, monkeypatch):
    from agentworks.execution._wsl2_platform_hold import decode_hold_payload, encode_hold_payload

    database, owner, _, _, carrier, _, _, span, _ = setup
    original = WSL2PlatformHold.start_recovery

    def substitute(self, deadline, **kwargs):
        ready = original(self, deadline, **kwargs)
        row = next(row for row in owner.list_lifecycle_obligations() if row.obligation_id == _HOLD_ID)
        payload = replace(decode_hold_payload(row.payload), nonce="f" * 32)
        database.operations.publish_lifecycle_obligation_payload(
            owner.ownership,
            _HOLD_ID,
            expected_revision=row.payload_revision,
            payload_version=row.payload_version,
            payload=encode_hold_payload(payload),
        )
        return ready

    monkeypatch.setattr(WSL2PlatformHold, "start_recovery", substitute)
    with pytest.raises(StateError):
        span.open(Deadline.after(10))
    assert carrier.calls == []


@pytest.mark.parametrize("unknown", [True, False])
def test_other_dispatch_or_unknown_attempt_prevents_availability_release(setup, unknown):
    database, owner, old_id, _, carrier, native, _, span, _ = setup
    span.open(Deadline.after(10))
    _resolve_old(database, owner, old_id)
    with span.action(Deadline.after(10)):
        row = owner.admit_recovery_support_obligation(
            "carrier-dispatch", payload_version=1, payload=b"", obligation_id="e" * 32
        )
        dispatch = owner.rebind_possible_effect_lifecycle_obligation(
            row.obligation_id, "carrier-dispatch", payload_version=1, payload=b"", payload_revision=0
        ).open_dispatch()
        if unknown:
            dispatch.begin_attempt()
    with pytest.raises(StateError):
        span.close(Deadline.after(10))
    assert "eof" not in native.events
    assert carrier.calls == ["guest", "admin", "root"]
    if not unknown:
        dispatch.begin_attempt().settle()
        dispatch.close()
        row.resolve()
        span.close(Deadline.after(10))
        assert "eof" in native.events


@pytest.mark.parametrize("change", ["stop", "route", "takeover"])
def test_hold_start_transition_changes_refuse_before_preparation(setup, monkeypatch, change):
    database, owner, _, platform, carrier, native, _, span, _ = setup
    original = WSL2PlatformHold.start_recovery

    def transition(self, deadline, **kwargs):
        ready = original(self, deadline, **kwargs)
        if change == "stop":
            database.set_operator_stopped("box", True)
            platform.observe_execution_power.return_value = VMStatus.STOPPED
        elif change == "route":
            platform.observe_provider_locator.return_value = ProviderLocator("changed")
        else:
            OperationOwner.recover(database.operations, owner.ownership, "f" * 32)
        return ready

    monkeypatch.setattr(WSL2PlatformHold, "start_recovery", transition)
    with pytest.raises((StateError, WSL2RouteRefusal)):
        span.open(Deadline.after(10))
    assert carrier.calls == []
    assert "dispatch" in native.events
    assert span.requires_owner_retention
