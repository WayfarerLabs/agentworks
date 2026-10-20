"""Namespace custody ordering, uncertainty and complete provider delivery bounds."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.db.operations import MAX_LIFECYCLE_PAYLOAD_BYTES, OperationRepository
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_gate_namespace as namespace
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_effect_gate import FileEffectGateBinding
from agentworks.execution._file_effect_gate_exchange import (
    GateControlCandidateResult,
    GateControlObservation,
    GateControlObservationState,
)
from agentworks.execution._file_gate_namespace import NamespacePreparation
from agentworks.execution._file_gate_namespace_bundle import ROOT_PROGRAM
from agentworks.execution._file_gate_namespace_exchange import NamespaceResult
from agentworks.execution._file_gate_namespace_obligation import (
    decode_namespace_obligation,
    encode_namespace_obligation,
)
from agentworks.execution._file_gate_namespace_protocol import MAX_REQUEST_BYTES, NamespaceStatus
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    _NumericGuestBootstrap,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, ExitStatus, FiniteInput, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.operations import OperationBorrow, OperationOwner
from agentworks.vms._native_operation import _RoutePreparation, _Workflow
from tests.execution.files.test_file_operations import SyntheticCarrier, _report

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from agentworks.execution._file_gate_namespace_obligation import NamespaceObligation
    from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier

_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
_AGENT = IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DEMOTE)
_TARGET = ManagedTargetIdentity(ManagedTargetKind.VM, "box", "v1:" + "b" * 64, vm_guest_boot_id(_GUEST))
_BOOTSTRAP = _NumericGuestBootstrap(_ROOT, _GUEST)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX)
_READY = RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3")


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[tuple[Database, OperationOwner]]:
    db = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(db.operations, OperationScope(OperationResourceKind.VM, "box"), "namespace")
    try:
        yield db, owner
    finally:
        db.close()


def _preparation(owner: OperationOwner, carrier: SyntheticCarrier | None = None) -> NamespacePreparation:
    return NamespacePreparation(
        owner, carrier or SyntheticCarrier(), _TARGET, _AGENT, _BOOTSTRAP, _RUNTIME, Deadline.after(10)
    )


def _install(monkeypatch: pytest.MonkeyPatch, owner: OperationOwner, status: NamespaceStatus | None) -> list[str]:
    events = []
    identity = None
    initial = None

    def setup(carrier: BorrowedFixedHelperCarrier, **kwargs) -> GateControlCandidateResult:
        nonlocal identity, initial
        rows = owner.list_pending_lifecycle_obligations()
        assert len(rows) == 1 and rows[0].obligation_kind == "file-gate-namespace"
        initial = decode_namespace_obligation(rows[0].payload)
        assert initial.binding is None and initial.setup.path == kwargs["path"]
        assert initial.bootstrap is not None and initial.bootstrap.guest == _GUEST
        identity = rows[0].obligation_id
        events.append("pending-before-setup")
        report = _report(carrier, kwargs["deadline"])
        binding = FileEffectGateBinding(kwargs["path"], b"i" * 16, b"g" * 16, _GUEST, 0, "box", 1, 2)
        return GateControlCandidateResult(
            report.dispatch,
            report.completion,
            0,
            None,
            _READY,
            GateControlObservation(GateControlObservationState.RESOLVED, binding=binding),
        )

    def effect(
        operation: BorrowedFixedHelperCarrier, record: NamespaceObligation, deadline: Deadline
    ) -> NamespaceResult:
        rows = owner.list_pending_lifecycle_obligations()
        assert len(rows) == 1 and rows[0].obligation_id == identity and rows[0].payload_revision == 1
        bound = decode_namespace_obligation(rows[0].payload)
        assert bound == record and bound.binding is not None and initial is not None
        assert bound.nonce == initial.nonce and bound.uid == initial.uid and bound.gid == initial.gid
        assert record.bootstrap == initial.bootstrap and record.runtime_selection == initial.runtime_selection
        events.append("bound-before-effects")
        report = _report(operation, deadline)
        return NamespaceResult(report.dispatch, report.completion, status)

    monkeypatch.setattr(namespace, "exchange_file_effect_gate", setup)
    monkeypatch.setattr(namespace, "exchange_namespace", effect)
    return events


@pytest.mark.parametrize("status", [NamespaceStatus.PREPARED, NamespaceStatus.REFUSED])
def test_single_row_and_borrow_ordering_and_authoritative_settlement(owned, monkeypatch, status):
    _, owner = owned
    events = _install(monkeypatch, owner, status)
    preparation = _preparation(owner)
    if status is NamespaceStatus.PREPARED:
        preparation.prepare()
    else:
        with pytest.raises(StateError):
            preparation.prepare()
    assert events == ["pending-before-setup", "bound-before-effects"]
    assert preparation.settled and preparation.prepared is (status is NamespaceStatus.PREPARED)
    assert not owner.list_pending_lifecycle_obligations()
    with pytest.raises(StateError):
        preparation.prepare()
    owner.borrow().close()


@pytest.mark.parametrize("status", [None, NamespaceStatus.UNCERTAIN])
def test_lost_reply_and_partial_effect_keep_exact_borrow_and_workflow(owned, monkeypatch, status):
    _, owner = owned
    events = _install(monkeypatch, owner, status)
    preparation = _preparation(owner)
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    with pytest.raises(StateError):
        preparation.prepare()
    row = owner.list_pending_lifecycle_obligations()[0]
    borrow = preparation._borrow
    assert borrow is not None and not preparation.settled
    with pytest.raises(StateError):
        workflow.close()
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(10))
    assert owner.list_pending_lifecycle_obligations()[0] == row
    assert preparation._borrow is borrow and len(events) == 2
    assert not workflow._components_settled()


@pytest.mark.parametrize("phase", ["setup", "binding", "effect"])
def test_control_escape_retains_wrapper_original_borrow_and_row(owned, monkeypatch, phase):
    _, owner = owned
    _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    control = KeyboardInterrupt()
    if phase == "binding":
        original = OperationRepository.publish_lifecycle_obligation_payload

        def publish(repository, *args, **kwargs):
            original(repository, *args, **kwargs)
            raise control

        monkeypatch.setattr(OperationRepository, "publish_lifecycle_obligation_payload", publish)
    else:
        original_exchange = getattr(
            namespace, "exchange_file_effect_gate" if phase == "setup" else "exchange_namespace"
        )

        def interrupt(*args, **kwargs):
            original_exchange(*args, **kwargs)
            raise control

        monkeypatch.setattr(
            namespace, "exchange_file_effect_gate" if phase == "setup" else "exchange_namespace", interrupt
        )
    preparation = _preparation(owner)
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    with pytest.raises(KeyboardInterrupt) as caught:
        preparation.prepare()
    assert caught.value is control
    with pytest.raises(StateError):
        workflow.close()
    rows = owner.list_pending_lifecycle_obligations()
    assert len(rows) == 1 and not preparation.settled and workflow.routes[0].namespace is preparation
    assert preparation._borrow is not None


def test_not_sent_setup_settles_unused_child_after_local_delivery(owned, monkeypatch):
    _, owner = owned
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    carrier = SyntheticCarrier(dispatch=Dispatch.NOT_SENT, completion=None)
    preparation = _preparation(owner, carrier)
    with pytest.raises(StateError):
        preparation.prepare()
    assert preparation.settled and not preparation.prepared
    assert events == ["pending-before-setup"] and not owner.list_pending_lifecycle_obligations()


def test_deadline_expires_between_binding_and_effects(owned, monkeypatch):
    _, owner = owned
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    original = vars(namespace)["exchange_file_effect_gate"]

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        object.__setattr__(kwargs["deadline"], "expires_at", 0.0)
        return result

    monkeypatch.setattr(namespace, "exchange_file_effect_gate", expire)
    preparation = _preparation(owner)
    with pytest.raises(StateError):
        preparation.prepare()
    assert events == ["pending-before-setup"]
    assert len(owner.list_pending_lifecycle_obligations()) == 1 and not preparation.settled


@pytest.mark.parametrize("phase", ["setup-not-sent", "prepared"])
def test_local_delivery_must_settle_before_unused_or_positive_child_retires(owned, monkeypatch, phase):
    _, owner = owned
    _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    carrier = SyntheticCarrier(
        dispatch=Dispatch.NOT_SENT if phase == "setup-not-sent" else Dispatch.SENT,
        completion=None if phase == "setup-not-sent" else ExitStatus(code=0),
    )
    preparation = _preparation(owner, carrier)
    selected = "exchange_file_effect_gate" if phase == "setup-not-sent" else "exchange_namespace"
    original = getattr(namespace, selected)
    closed = True

    def pending_delivery(*args, **kwargs):
        nonlocal closed
        result = original(*args, **kwargs)
        closed = False
        return result

    monkeypatch.setattr(namespace, selected, pending_delivery)
    monkeypatch.setattr(LocalDeliveryCustody, "settled", property(lambda self: closed))
    monkeypatch.setattr(LocalDeliveryCustody, "close", lambda self, deadline: closed)
    with pytest.raises(StateError):
        preparation.prepare()
    row = owner.list_pending_lifecycle_obligations()[0]
    borrow = preparation._borrow
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    with pytest.raises(StateError):
        workflow.close()
    assert owner.list_pending_lifecycle_obligations()[0] == row and preparation._borrow is borrow
    assert not preparation.settled
    closed = True
    workflow.close(cleanup_deadline=Deadline.after(10))
    assert preparation.settled and preparation.prepared is (phase == "prepared")
    assert owned[0].operations.inspect(owner.ownership.scope) is None


def test_registration_commit_reply_loss_keeps_original_identity_before_setup(owned, monkeypatch):
    _, owner = owned
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    original = OperationRepository.register_lifecycle_obligation
    control = KeyboardInterrupt()

    def register(repository, *args, **kwargs):
        original(repository, *args, **kwargs)
        raise control

    monkeypatch.setattr(OperationRepository, "register_lifecycle_obligation", register)
    preparation = _preparation(owner)
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare()
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    with pytest.raises(StateError):
        workflow.close()
    rows = owner.list_pending_lifecycle_obligations()
    assert len(rows) == 1 and rows[0].obligation_id == preparation._obligation_id and not events
    assert preparation._borrow is not None and not preparation.settled


def test_positive_result_survives_lost_durable_close_reply_without_replay(owned, monkeypatch):
    _, owner = owned
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    original = OperationRepository.resolve_lifecycle_obligation
    control = KeyboardInterrupt()
    replies = 0

    def resolve(repository, *args, **kwargs):
        nonlocal replies
        result = original(repository, *args, **kwargs)
        replies += 1
        if replies == 1:
            raise control
        return result

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", resolve)
    preparation = _preparation(owner)
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare()
    assert preparation._borrow is not None and not preparation.settled
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    workflow.close()
    assert preparation.settled and preparation.prepared and len(events) == 2
    assert owned[0].operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize("cleanup_control", [None, StateError, KeyboardInterrupt, SystemExit])
def test_absent_registration_refusal_retires_exact_unused_borrow_and_retries(owned, monkeypatch, cleanup_control):
    _, owner = owned
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    refusal = ValidationError("registration refused")
    cleanup = (cleanup_control or KeyboardInterrupt)("unused close reply")
    original_close = OperationBorrow.close
    close_calls = 0

    def register(*args, **kwargs):
        raise refusal

    def close(borrow):
        nonlocal close_calls
        close_calls += 1
        if cleanup_control and close_calls == 1:
            raise cleanup
        original_close(borrow)

    monkeypatch.setattr(OperationRepository, "register_lifecycle_obligation", register)
    monkeypatch.setattr(OperationBorrow, "close", close)
    preparation = _preparation(owner)
    preserves_cleanup = cleanup_control in (KeyboardInterrupt, SystemExit)
    with pytest.raises(cleanup_control if preserves_cleanup else ValidationError) as caught:
        preparation.prepare()
    assert caught.value is (cleanup if preserves_cleanup else refusal)
    assert not events and not owner.list_pending_lifecycle_obligations()
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    workflow.close()
    assert preparation.settled and not preparation.prepared
    assert preparation._borrow is not None and preparation._borrow.closed
    assert owned[0].operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize("case", ["committed-error", "unreadable", "registration-control"])
def test_unproved_registration_stays_retained_after_cleanup_retry(owned, monkeypatch, case):
    _, owner = owned
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    original = OperationRepository.register_lifecycle_obligation
    control = KeyboardInterrupt() if case == "registration-control" else ValidationError("registration reply")

    def register(repository, *args, **kwargs):
        if case == "committed-error":
            original(repository, *args, **kwargs)
        raise control

    def unreadable(*args, **kwargs):
        raise StateError("receipt read unavailable")

    monkeypatch.setattr(OperationRepository, "register_lifecycle_obligation", register)
    if case == "unreadable":
        monkeypatch.setattr(OperationRepository, "inspect_lifecycle_obligation", unreadable)
    preparation = _preparation(owner)
    with pytest.raises(type(control)) as caught:
        preparation.prepare()
    assert caught.value is control
    workflow = _Workflow(owner, Deadline.after(10), routes=[_RoutePreparation(namespace=preparation)])
    for _ in range(2):
        with pytest.raises(StateError):
            workflow.close(cleanup_deadline=Deadline.after(10))
    assert not preparation.settled and not events
    assert preparation._borrow is not None and not preparation._borrow.closed
    assert owned[0].operations.inspect(owner.ownership.scope) is not None


def _at_payload_boundary(owner: OperationOwner) -> dict[str, Any]:
    record = _preparation(owner)._record
    binding = FileEffectGateBinding(record.setup.path, b"i" * 16, b"g" * 16, _GUEST, 0, "box", 2**64 - 1, 2**64 - 1)
    value: dict[str, Any] = json.loads(encode_namespace_obligation(replace(record, binding=binding)))

    def encoded() -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    count = 1
    while True:
        value["bootstrap"]["root_entry"]["groups"] = list(range(count + 1))
        if len(encoded()) > MAX_LIFECYCLE_PAYLOAD_BYTES:
            break
        count += 1
    value["bootstrap"]["root_entry"]["groups"] = list(range(count))
    gap = MAX_LIFECYCLE_PAYLOAD_BYTES - len(encoded())
    if gap:
        last = value["bootstrap"]["root_entry"]["groups"][-1]
        value["bootstrap"]["root_entry"]["groups"][-1] = 10 ** (len(str(last)) + gap - 1)
    assert len(encoded()) == MAX_LIFECYCLE_PAYLOAD_BYTES
    return value


def test_namespace_admission_reserves_largest_binding_before_borrow(owned, monkeypatch):
    _, owner = owned
    value = _at_payload_boundary(owner)
    value["bootstrap"]["root_entry"]["groups"][-1] *= 10
    bootstrap = _NumericGuestBootstrap(
        replace(_ROOT, expected=replace(_ROOT.expected, groups=tuple(value["bootstrap"]["root_entry"]["groups"]))),
        _GUEST,
    )
    payload = encode_namespace_obligation(replace(_preparation(owner)._record, bootstrap=bootstrap))
    assert len(payload) < MAX_LIFECYCLE_PAYLOAD_BYTES
    record = decode_namespace_obligation(payload)
    carrier = SyntheticCarrier()
    monkeypatch.setattr(owner, "borrow", lambda: pytest.fail("oversized binding reached borrow"))
    with pytest.raises(ValueError):
        NamespacePreparation(owner, carrier, _TARGET, _AGENT, record.bootstrap, _RUNTIME, Deadline.after(10))
    assert carrier.calls == 0


def test_namespace_largest_supported_binding_fits_actual_publication(owned, monkeypatch):
    _, owner = owned
    value = _at_payload_boundary(owner)
    bound_payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    bound = decode_namespace_obligation(bound_payload)
    events = _install(monkeypatch, owner, NamespaceStatus.PREPARED)
    original = vars(namespace)["exchange_file_effect_gate"]

    def setup(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, observation=replace(result.observation, binding=bound.binding))

    monkeypatch.setattr(namespace, "exchange_file_effect_gate", setup)
    preparation = NamespacePreparation(
        owner, SyntheticCarrier(), _TARGET, _AGENT, bound.bootstrap, _RUNTIME, Deadline.after(10)
    )
    preparation.prepare()
    assert preparation.settled and preparation.prepared and len(events) == 2
    value["bootstrap"]["root_entry"]["groups"][-1] *= 10
    with pytest.raises(ValueError):
        decode_namespace_obligation(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


@pytest.mark.parametrize(
    "target",
    [
        {"kind": "platform-host", "name": "box"},
        {"kind": "platform-host", "name": "box", "incarnation": _TARGET.incarnation, "boot_id": _TARGET.boot_id},
    ],
)
def test_namespace_decoder_refuses_persisted_host_targets(
    owned: tuple[Database, OperationOwner], target: dict[str, str]
) -> None:
    _, owner = owned
    record = _preparation(owner)._record
    value = json.loads(encode_namespace_obligation(record))
    value["target"] = target
    with pytest.raises(ValueError):
        decode_namespace_obligation(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def test_durable_codec_is_closed_bounded_and_retains_exact_binding(owned, monkeypatch):
    _, owner = owned
    _install(monkeypatch, owner, NamespaceStatus.UNCERTAIN)
    preparation = _preparation(owner)
    with pytest.raises(StateError):
        preparation.prepare()
    payload = owner.list_pending_lifecycle_obligations()[0].payload
    record = decode_namespace_obligation(payload)
    assert encode_namespace_obligation(record) == payload and len(payload) < MAX_LIFECYCLE_PAYLOAD_BYTES
    value = json.loads(payload)
    value["account"] = "unexpected"
    with pytest.raises(ValueError):
        decode_namespace_obligation(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    with pytest.raises(ValueError):
        decode_namespace_obligation(payload + b" " * MAX_LIFECYCLE_PAYLOAD_BYTES)
    assert record.binding is not None
    with pytest.raises(ValueError):
        encode_namespace_obligation(replace(record, binding=replace(record.binding, scope_name="other")))


@pytest.mark.parametrize("root_mode", [IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_complete_qga_body_bound_and_oversize_refusal(root_mode, monkeypatch):
    argv, _, _ = build_root_guest_bootstrap_argv(
        replace(_ROOT, mode=root_mode),
        _ROOT.expected,
        selection=_RUNTIME,
        program=ROOT_PROGRAM,
        nonce="0" * 32,
        expected_guest=_GUEST,
    )
    invocation = PreparedInvocation(argv)
    io = CarrierIO(input=FiniteInput(ROOT_PROGRAM.prefix + b"x" * MAX_REQUEST_BYTES))
    body = ProxmoxCarrier._request_body(invocation, io)
    assert len(body) < 65_536
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.example:8006", "node", 101, "root@pam!token", "secret"))

    def wire(*args, **kwargs):
        pytest.fail("oversized namespace request reached provider")

    monkeypatch.setattr(carrier._wire, "request", wire)
    with pytest.raises(ValidationError):
        carrier.execute(
            invocation,
            io=CarrierIO(input=FiniteInput(b"x" * 65_536)),
            deadline=Deadline.after(1),
            custody=LocalDeliveryCustody(),
        )
