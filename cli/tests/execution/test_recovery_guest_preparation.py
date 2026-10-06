"""SQLite custody and synthetic native transitions for fixed recovery probes.

These fixtures exercise real fixed observation protocols, not native endpoint
discovery, availability or drain evidence.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import Mock

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable, VMPlatform
from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._account_protocol import AccountFailure, encode_account_failure, encode_account_identity
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._recovery_guest_preparation import (
    RecoveryGuestPreparationBatch,
    RecoveryGuestPreparationControlFact,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._target_identity import TargetIdentityFailure, TargetIdentityStatus
from agentworks.execution._vm_guest_identity_protocol import (
    VMGuestIdentity,
    VMGuestIdentityFailure,
    encode_vm_guest_identity_failure,
    encode_vm_guest_identity_success,
)
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.operations import (
    LifecycleObligation,
    OperationOwner,
    RecoveredLifecycleObligation,
    RecoveryAttempt,
    RecoveryDispatch,
)
from agentworks.vms.target_preparation import VMTargetPreparationFailure, VMTargetPreparationStatus
from tests.vms.test_target_preparation import _vm

pytestmark = pytest.mark.windows

_ROOT = IdentityExpectation(0, 0, (0,))
_ADMIN = IdentityExpectation(1001, 1002, (1002, 1003))
_WORKER = IdentityExpectation(2001, 2002, (2002,))
_LOCATOR = ProviderLocator("opaque")
_GUEST = VMGuestIdentity("0123456789abcdef0123456789abcdef", "00000000-0000-4000-8000-000000000001", 1234)
_BATCH_ID = "c" * 32


class ControlStop(BaseException):
    pass


class FixedCarrier:
    """Emit guest/account protocol fixtures through their actual bounded sinks."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.invocations: list[PreparedInvocation] = []
        self.deadlines: list[Deadline] = []
        self.identities = {"admin": _ADMIN, "worker": _WORKER, "root": _ROOT}
        self.refuse: str | None = None
        self.malformed: str | None = None
        self.runtime: str | None = None
        self.not_sent: str | None = None
        self.abnormal: str | None = None
        self.control: str | None = None
        self.expire: str | None = None
        self.after = Mock()

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
        account = json.loads(io.input.data)["account"] if isinstance(io.input, FiniteInput) else "guest"
        self.calls.append(account)
        self.invocations.append(invocation)
        self.deadlines.append(deadline)
        if account == self.control:
            raise ControlStop
        assert isinstance(io.output, SinkOutput)
        if account == self.refuse:
            response = (
                encode_vm_guest_identity_failure(nonce, VMGuestIdentityFailure.MARKER_MISSING)
                if account == "guest"
                else encode_account_failure(nonce, AccountFailure.MISSING)
            )
        elif account == self.malformed:
            response = b'{"invalid":true}'
        else:
            response = (
                encode_vm_guest_identity_success(nonce, _GUEST)
                if account == "guest"
                else encode_account_identity(nonce, self.identities[account])
            )
        prefix = b"invalid runtime\n" if account == self.runtime else f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode("ascii")
        dispatch = Dispatch.NOT_SENT if account == self.not_sent else Dispatch.SENT
        if dispatch is Dispatch.SENT:
            io.output.stdout.try_write(memoryview(prefix + response))
        if account == self.expire:
            object.__setattr__(deadline, "expires_at", 0.0)
        self.after(account)
        complete = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(
            dispatch,
            None if account == self.abnormal or dispatch is Dispatch.NOT_SENT else ExitStatus(code=0),
            stdout=complete,
            stderr=complete,
        )


@pytest.fixture
def recovery(tmp_path: Path):
    database = Database(tmp_path / "state.db")
    predecessor = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "box"), "file-download"
    )
    old = predecessor.register_lifecycle_obligation("file", payload_version=1, payload=b"old")
    old.mark_possible_effect()
    owner = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
    hold = owner.admit_recovery_support_obligation(
        "hold", payload_version=1, payload=b"ready fixture", obligation_id="d" * 32
    )
    try:
        yield database, owner, old.obligation_id, hold
    finally:
        database.close()


def _batch(owner: OperationOwner, carrier: FixedCarrier, *, delivery: str = "admin", early: FixedCarrier | None = None):
    local_delivery = LocalDeliveryCustody()
    route = None if early is None else _EarlyGuestFactsRoute(early, IdentityPlan(_ROOT, IdentityMode.DIRECT), delivery)
    binding = NativeExecutionBinding(
        carrier, delivery, RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"), route
    )
    return RecoveryGuestPreparationBatch(binding, owner, _BATCH_ID, provider_custody=local_delivery)


def _prepare(batch: RecoveryGuestPreparationBatch, *, workload: str = "admin", elevated: bool = True, **kwargs):
    platform = Mock(spec=VMPlatform)
    platform.site_name = "local"
    platform.observe_provider_locator.return_value = _LOCATOR
    return batch.prepare(
        kwargs.pop("vm", _vm()),
        kwargs.pop("platform", platform),
        None,
        _LOCATOR,
        workload_account=workload,
        include_elevated=elevated,
        deadline=kwargs.pop("deadline", Deadline.after(10)),
        **kwargs,
    )


def _row(owner: OperationOwner):
    return next(row for row in owner.list_lifecycle_obligations() if row.obligation_id == _BATCH_ID)


def test_distinct_batch_debt_and_selected_guest_route(recovery):
    database, owner, old_id, hold = recovery
    carrier, early = FixedCarrier(), FixedCarrier()
    batch = _batch(owner, carrier, early=early)
    seen = []

    def during_probe(account):
        row = _row(owner)
        seen.append(account)
        assert row.ownership == owner.ownership
        assert (row.obligation_kind, row.payload_version, row.payload, row.payload_revision) == (
            "carrier-dispatch",
            1,
            b"",
            0,
        )
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        for transition in (
            hold.resolve,
            owner.record_effects_resolved,
            lambda: hold.publish_payload(expected_revision=0, payload_version=1, payload=b"changed"),
        ):
            with pytest.raises(StateError):
                transition()

    early.after.side_effect = during_probe
    carrier.after.side_effect = during_probe
    deadline = Deadline.after(10)
    result = _prepare(batch, deadline=deadline)
    assert early.calls == ["guest"]
    assert carrier.calls == ["admin", "root"]
    assert seen == ["guest", "admin", "root"]
    assert all(value is deadline for value in early.deadlines + carrier.deadlines)
    assert result.guest.status is VMTargetPreparationStatus.PREPARED
    assert result.identity.status is TargetIdentityStatus.PREPARED
    assert result.identity.ordinary_plan == IdentityPlan(_ADMIN, IdentityMode.DIRECT)
    assert result.identity.elevated_plan == IdentityPlan(_ROOT, IdentityMode.SUDO_ROOT)
    assert result.identity.delivery_result is result.identity.workload_result
    assert not result.requires_owner_retention
    rows = {row.obligation_id: row for row in owner.list_lifecycle_obligations()}
    assert rows[_BATCH_ID].state is LifecycleObligationState.RESOLVED
    assert rows[old_id].state is rows[hold.obligation_id].state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        _prepare(batch)


@pytest.mark.parametrize(
    "delivery,workload,elevated,mode,calls",
    [
        ("root", "worker", True, IdentityMode.DEMOTE, ["guest", "root", "worker"]),
        ("root", "root", True, IdentityMode.DIRECT, ["guest", "root"]),
        ("admin", "admin", False, IdentityMode.DIRECT, ["guest", "admin"]),
        ("admin", "worker", True, None, ["guest", "admin", "worker"]),
    ],
)
def test_actual_numeric_plans_and_per_batch_cache(recovery, delivery, workload, elevated, mode, calls):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    result = _prepare(_batch(owner, carrier, delivery=delivery), workload=workload, elevated=elevated)
    assert carrier.calls == calls
    assert not result.requires_owner_retention
    if mode is None:
        assert result.identity.status is TargetIdentityStatus.FAILED
        assert result.identity.failure is TargetIdentityFailure.IDENTITY_PATH
    else:
        assert result.identity.status is TargetIdentityStatus.PREPARED
        assert result.identity.ordinary_plan.mode is mode


@pytest.mark.parametrize("account", ["guest", "admin", "root"])
@pytest.mark.parametrize("fault", ["refuse", "malformed", "runtime", "not_sent", "abnormal", "expire", "control"])
def test_protocol_failures_and_unknown_custody_stop_next_probe(recovery, account, fault):
    _, owner, _, hold = recovery
    carrier = FixedCarrier()
    setattr(carrier, fault, account)
    batch = _batch(owner, carrier)
    if fault == "control":
        with pytest.raises(ControlStop) as stopped:
            _prepare(batch)
        assert isinstance(stopped.value.__cause__, RecoveryGuestPreparationControlFact)
        result = stopped.value.__cause__.preparation
    else:
        result = _prepare(batch)
    assert carrier.calls == ["guest", "admin", "root"][: ["guest", "admin", "root"].index(account) + 1]
    unknown = fault in ("abnormal", "control")
    assert result.requires_owner_retention is unknown
    assert result.pending_remote_effects is unknown
    assert _row(owner).state is (
        LifecycleObligationState.POSSIBLE_EFFECT if unknown else LifecycleObligationState.RESOLVED
    )
    if unknown:
        with pytest.raises(StateError):
            hold.resolve()
        with pytest.raises(StateError):
            batch.retry_resolution()
    elif fault == "not_sent":
        if account == "guest":
            assert result.guest is not None
            assert result.guest.failure is VMTargetPreparationFailure.DISPATCH
        else:
            assert result.identity is not None
            assert result.identity.failure is TargetIdentityFailure.DISPATCH
    if account != "guest" and fault != "control":
        assert result.identity is not None
        assert result.identity.delivery_result is not None


@pytest.mark.parametrize("when", ["before", "after", "unavailable"])
def test_selected_locator_failures_skip_accounts(recovery, when):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    platform = Mock(spec=VMPlatform)
    platform.site_name = "local"
    changed = ProviderLocator("changed")
    platform.observe_provider_locator.side_effect = {
        "before": [changed],
        "after": [_LOCATOR, changed],
        "unavailable": [_LOCATOR, ProviderLocatorUnavailable()],
    }[when]
    result = _prepare(_batch(owner, carrier), platform=platform)
    assert carrier.calls == ([] if when == "before" else ["guest"])
    assert result.identity is None
    assert result.guest.status is VMTargetPreparationStatus.FAILED
    assert not result.requires_owner_retention


@pytest.mark.parametrize("missing", [True, False])
def test_marker_and_deadline_preflight_resolve_without_dispatch(recovery, missing):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    result = _prepare(
        _batch(owner, carrier),
        vm=_vm(marker=None) if missing else _vm(),
        deadline=Deadline.after(10) if missing else Deadline.after(0),
    )
    assert carrier.calls == []
    assert result.guest.failure is (
        VMTargetPreparationFailure.MARKER_MISSING if missing else VMTargetPreparationFailure.DEADLINE
    )
    assert _row(owner).state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("transition", ["admission", "open", "begin", "settle", "close", "resolve"])
@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_bookkeeping_retains_and_resolution_retry_never_replays(
    recovery, monkeypatch, transition, committed
):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier)
    cls, method = {
        "admission": (OperationOwner, "admit_recovery_support_obligation"),
        "open": (RecoveredLifecycleObligation, "open_dispatch"),
        "begin": (RecoveryDispatch, "begin_attempt"),
        "settle": (RecoveryAttempt, "settle"),
        "close": (RecoveryDispatch, "close"),
        "resolve": (LifecycleObligation, "resolve"),
    }[transition]
    original = getattr(cls, method)

    def interrupted(self, *args, **kwargs):
        if committed:
            original(self, *args, **kwargs)
        raise ControlStop

    with monkeypatch.context() as patch:
        patch.setattr(cls, method, interrupted)
        with pytest.raises(ControlStop) as stopped:
            _prepare(batch)
    assert isinstance(stopped.value.__cause__, RecoveryGuestPreparationControlFact)
    assert batch.preparation.requires_owner_retention
    assert batch.preparation.coordination_uncertain
    if transition == "settle":
        assert batch.preparation.guest.guest_result is not None
    expected = {
        "admission": [],
        "open": [],
        "begin": [],
        "settle": ["guest"],
        "close": ["guest", "admin", "root"],
        "resolve": ["guest", "admin", "root"],
    }[transition]
    assert carrier.calls == expected
    if transition in ("open", "close", "resolve"):
        assert not batch.retry_resolution().requires_owner_retention
        assert _row(owner).state is LifecycleObligationState.RESOLVED
        assert carrier.calls == expected
    else:
        with pytest.raises(StateError):
            batch.retry_resolution()
    with pytest.raises(StateError):
        _prepare(batch)


def test_takeover_between_probes_stops_and_preserves_batch(recovery):
    database, owner, old_id, hold = recovery
    carrier = FixedCarrier()
    successor = None

    def takeover(account):
        nonlocal successor
        if account == "guest":
            successor = OperationOwner.recover(database.operations, owner.ownership, "e" * 32)

    carrier.after.side_effect = takeover
    batch = _batch(owner, carrier)
    with pytest.raises(StateError) as stopped:
        _prepare(batch)
    assert isinstance(stopped.value.__cause__, RecoveryGuestPreparationControlFact)
    assert carrier.calls == ["guest"]
    assert batch.preparation.requires_owner_retention
    assert successor is not None
    rows = successor.list_lifecycle_obligations()
    assert {row.obligation_id for row in rows} == {old_id, hold.obligation_id, _BATCH_ID}
    assert all(row.state is LifecycleObligationState.POSSIBLE_EFFECT for row in rows)
    assert all(row.ownership == successor.ownership for row in rows)
    database.operations.resolve_lifecycle_obligation(successor.ownership, old_id)
    database.operations.resolve_lifecycle_obligation(successor.ownership, hold.obligation_id)
    with pytest.raises(StateError):
        successor.record_effects_resolved()
    assert _row(successor).state is LifecycleObligationState.POSSIBLE_EFFECT


def test_fresh_identifier_and_pinned_api(recovery):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier)
    with pytest.raises(TypeError):
        _prepare(batch, carrier=FixedCarrier())
    with pytest.raises(TypeError):
        _prepare(batch, runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/other"))
    owner.admit_recovery_support_obligation("carrier-dispatch", payload_version=1, payload=b"", obligation_id=_BATCH_ID)
    with pytest.raises(StateError):
        _prepare(batch)
    assert carrier.calls == []


def test_boundary_validation_precedes_admission(recovery):
    _, owner, _, _ = recovery
    batch = _batch(owner, FixedCarrier())
    with pytest.raises(ValidationError):
        _prepare(batch, vm=replace(_vm(), name="different"))
    with pytest.raises(ValidationError):
        _prepare(batch, deadline=Deadline.after(None))
    assert _BATCH_ID not in {row.obligation_id for row in owner.list_lifecycle_obligations()}


@pytest.mark.parametrize("change", ["revision", "resolved"])
def test_every_probe_revalidates_the_exact_possible_row(recovery, change):
    database, owner, _, _ = recovery
    carrier = FixedCarrier()

    def change_row(account):
        if account != "guest":
            return
        # Deliberately bypass local coordination to model persisted interference.
        if change == "revision":
            database.operations.publish_lifecycle_obligation_payload(
                owner.ownership, _BATCH_ID, expected_revision=0, payload_version=1, payload=b"changed"
            )
        else:
            database.operations.resolve_lifecycle_obligation(owner.ownership, _BATCH_ID)

    carrier.after.side_effect = change_row
    batch = _batch(owner, carrier)
    with pytest.raises(StateError) as stopped:
        _prepare(batch)
    assert isinstance(stopped.value.__cause__, RecoveryGuestPreparationControlFact)
    assert carrier.calls == ["guest"]
    assert batch.preparation.coordination_uncertain


def test_deadline_after_locator_confirmation_skips_numeric_queries(recovery):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    deadline = Deadline.after(10)
    platform = Mock(spec=VMPlatform)
    platform.site_name = "local"
    count = 0

    def locator(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            object.__setattr__(deadline, "expires_at", 0.0)
        return _LOCATOR

    platform.observe_provider_locator.side_effect = locator
    result = _prepare(_batch(owner, carrier), platform=platform, deadline=deadline)
    assert carrier.calls == ["guest"]
    assert result.guest.failure is VMTargetPreparationFailure.DEADLINE
    assert result.identity is None
    assert not result.requires_owner_retention


@pytest.mark.parametrize("account", ["admin", "worker", "root"])
def test_numeric_observations_survive_settlement_interruption(recovery, monkeypatch, account):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier, delivery="root" if account == "worker" else "admin")
    original = RecoveryAttempt.settle

    def settle(self):
        if carrier.calls[-1] == account:
            raise ControlStop
        original(self)

    monkeypatch.setattr(RecoveryAttempt, "settle", settle)
    with pytest.raises(ControlStop) as stopped:
        _prepare(batch, workload="worker" if account == "worker" else "admin")
    assert isinstance(stopped.value.__cause__, RecoveryGuestPreparationControlFact)
    fact = stopped.value.__cause__.preparation
    assert fact.identity is not None
    observation = {
        "admin": fact.identity.delivery_result,
        "worker": fact.identity.workload_result,
        "root": fact.identity.root_result,
    }[account]
    assert observation is not None and observation.observation is not None
    assert observation.observation.identity == carrier.identities[account]
    assert fact.coordination_uncertain and fact.requires_owner_retention


@pytest.mark.parametrize("transition", ["open_candidate", "partial_close"])
def test_partial_local_transition_retries_resolution_without_probes(recovery, transition):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier)

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        if event != "line":
            return interrupt
        if transition == "open_candidate" and frame.f_code is RecoveredLifecycleObligation.open_dispatch.__code__:
            binding = batch._recovered  # noqa: SLF001
            if binding is not None and binding._local_dispatch is not None and owner._active_recovery_dispatch is None:  # noqa: SLF001
                raise KeyboardInterrupt
        if transition == "partial_close" and frame.f_code is RecoveryDispatch.close.__code__:
            dispatch = batch._dispatch  # noqa: SLF001
            if dispatch is not None and dispatch._close_started and owner._active_recovery_dispatch is None:  # noqa: SLF001
                raise KeyboardInterrupt
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            _prepare(batch)
    finally:
        sys.settrace(None)
    expected = [] if transition == "open_candidate" else ["guest", "admin", "root"]
    assert carrier.calls == expected
    assert batch.preparation.requires_owner_retention
    assert not batch.retry_resolution().requires_owner_retention
    assert _row(owner).state is LifecycleObligationState.RESOLVED
    assert carrier.calls == expected


def test_interrupted_unreturned_open_cleanup_keeps_exact_custody(recovery, monkeypatch):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier)
    original_open = RecoveredLifecycleObligation.open_dispatch
    original_cleanup = RecoveredLifecycleObligation._close_retained_dispatch  # noqa: SLF001

    def open_then_interrupt(self):
        original_open(self)
        raise ControlStop

    def cleanup_then_interrupt(self):
        original_cleanup(self)
        raise ControlStop

    with monkeypatch.context() as patch:
        patch.setattr(RecoveredLifecycleObligation, "open_dispatch", open_then_interrupt)
        with pytest.raises(ControlStop):
            _prepare(batch)
        patch.setattr(RecoveredLifecycleObligation, "_close_retained_dispatch", cleanup_then_interrupt)
        with pytest.raises(ControlStop):
            batch.retry_resolution()
    assert batch.preparation.coordination_uncertain
    assert _row(owner).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert not batch.retry_resolution().requires_owner_retention
    assert carrier.calls == []


def test_local_close_retry_cannot_resolve_after_takeover(recovery, monkeypatch):
    database, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier)
    original = RecoveryDispatch.close

    def close_then_interrupt(self):
        original(self)
        raise ControlStop

    with monkeypatch.context() as patch:
        patch.setattr(RecoveryDispatch, "close", close_then_interrupt)
        with pytest.raises(ControlStop):
            _prepare(batch)
    successor = OperationOwner.recover(database.operations, owner.ownership, "f" * 32)
    with pytest.raises(StateError):
        batch.retry_resolution()
    assert batch.preparation.requires_owner_retention
    assert _row(successor).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert carrier.calls == ["guest", "admin", "root"]


def test_post_resolution_interruption_retains_until_local_normalization(recovery, monkeypatch):
    _, owner, _, _ = recovery
    carrier = FixedCarrier()
    batch = _batch(owner, carrier)

    def interrupt(frame: FrameType, event: str, arg: object) -> Any:
        del arg
        if (
            event == "line"
            and frame.f_code is RecoveryGuestPreparationBatch._finish_settled_batch.__code__  # noqa: SLF001
            and batch._resolved  # noqa: SLF001
        ):
            raise KeyboardInterrupt
        return interrupt

    sys.settrace(interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as stopped:
            _prepare(batch)
    finally:
        sys.settrace(None)
    assert isinstance(stopped.value.__cause__, RecoveryGuestPreparationControlFact)
    fact = stopped.value.__cause__.preparation
    assert fact.coordination_uncertain and fact.requires_owner_retention
    assert _row(owner).state is LifecycleObligationState.RESOLVED
    dispatch = batch._dispatch  # noqa: SLF001
    assert dispatch is not None
    with pytest.raises(StateError):
        dispatch.begin_attempt()
    rows = owner.list_lifecycle_obligations()
    monkeypatch.setattr(LifecycleObligation, "resolve", Mock(side_effect=AssertionError("Unexpected resolution")))
    monkeypatch.setattr(RecoveryDispatch, "close", Mock(side_effect=AssertionError("Unexpected close")))
    normalized = batch.retry_resolution()
    assert batch._dispatch is None  # noqa: SLF001
    assert not normalized.coordination_uncertain
    assert not normalized.requires_owner_retention
    assert owner.list_lifecycle_obligations() == rows
    assert carrier.calls == ["guest", "admin", "root"]
