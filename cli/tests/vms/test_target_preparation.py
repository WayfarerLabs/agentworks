"""Owned managed VM target preparation behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope, VMRow
from agentworks.errors import StateError, ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity_protocol import (
    VMGuestIdentity,
    VMGuestIdentityFailure,
    encode_vm_guest_identity_failure,
    encode_vm_guest_identity_success,
)
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.operations import OperationOwner
from agentworks.vms.target_preparation import (
    VMTargetPreparationControlFact,
    VMTargetPreparationFailure,
    VMTargetPreparationStatus,
    prepare_managed_vm_target,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


_MARKER = "0123456789abcdef0123456789abcdef"
_OTHER_MARKER = "fedcba9876543210fedcba9876543210"
_BOOT_ID = "00000000-0000-4000-8000-000000000001"
_NONCE_MARKER = "agentworks-runtime-prerequisite"


def _vm(*, marker: str | None = _MARKER) -> VMRow:
    return VMRow(
        name="box",
        site="local",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="ready",
        init_status="ready",
        tailscale_host=None,
        cpus=None,
        memory_gib=None,
        disk_gib=None,
        swap_gib=None,
        admin_username="admin",
        hostname="box",
        created_at="2026-09-21T00:00:00Z",
        last_seen_at=None,
        instance_marker=marker,
    )


def _runtime() -> RuntimeSelection:
    return RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")


def _binding(carrier: TranscriptCarrier) -> NativeExecutionBinding:
    return NativeExecutionBinding(carrier, "admin", _runtime())


def _nonce(invocation: PreparedInvocation) -> str:
    position = invocation.argv.index(_NONCE_MARKER)
    return invocation.argv[position + 1]


@dataclass
class TranscriptCarrier:
    payload: bytes = b""
    report: CarrierReport = CarrierReport(
        Dispatch.SENT,
        ExitStatus(code=0),
        stdout=CapturedOutput(complete=True, retention=Retention.DELIVERED),
        stderr=CapturedOutput(complete=True, retention=Retention.DELIVERED),
    )
    control: BaseException | None = None
    expire_on_return: bool = False
    calls: int = 0
    deadlines: list[Deadline] | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        if self.deadlines is not None:
            self.deadlines.append(deadline)
        if self.control is not None:
            raise self.control
        assert isinstance(io.output, SinkOutput)
        nonce = _nonce(invocation)
        if self.report.dispatch is Dispatch.SENT:
            prefix = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode("ascii")
            io.output.stdout.try_write(memoryview(prefix + self.payload.replace(b"0" * 32, nonce.encode())))
        if self.expire_on_return:
            object.__setattr__(deadline, "expires_at", 0.0)
        return self.report


class ControlStop(BaseException):
    pass


@pytest.fixture
def owned(tmp_path: Path) -> Generator[tuple[Database, OperationOwner], None, None]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "box"),
        "target-preparation",
    )
    try:
        yield database, owner
    finally:
        database.close()


def _prepare(
    owner: OperationOwner,
    carrier: TranscriptCarrier,
    *,
    vm: VMRow | None = None,
    locator: ProviderLocator | ProviderLocatorUnavailable | None = None,
    deadline: Deadline | None = None,
):
    return prepare_managed_vm_target(
        vm or _vm(),
        ProviderLocator("opaque") if locator is None else locator,
        _binding(carrier),
        deadline=deadline or Deadline.after(10),
        owner=owner,
    )


def _success_payload(marker: str = _MARKER) -> bytes:
    return encode_vm_guest_identity_success("0" * 32, VMGuestIdentity(marker, _BOOT_ID))


def test_prepares_identity_under_one_borrow_and_leaves_owner_open(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner = owned
    carrier = TranscriptCarrier(_success_payload(), deadlines=[])
    calls = 0
    original = OperationOwner.borrow

    def borrow(self: OperationOwner):
        nonlocal calls
        if self is owner:
            calls += 1
        return original(self)

    monkeypatch.setattr(OperationOwner, "borrow", borrow)
    deadline = Deadline.after(10)
    result = _prepare(owner, carrier, deadline=deadline)

    assert result.status is VMTargetPreparationStatus.PREPARED
    assert result.target is not None
    assert result.target.name == "box"
    assert result.guest_result is not None
    assert result.failure is None
    assert carrier.calls == 1
    assert carrier.deadlines == [deadline]
    assert calls == 1
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    follow_up = owner.borrow()
    follow_up.close()
    owner.close()


@pytest.mark.parametrize(
    ("vm", "locator", "deadline", "failure", "expired"),
    [
        (
            _vm(),
            ProviderLocatorUnavailable(),
            Deadline.after(10),
            VMTargetPreparationFailure.LOCATOR_UNAVAILABLE,
            False,
        ),
        (
            _vm(marker=None),
            ProviderLocator("opaque"),
            Deadline.after(10),
            VMTargetPreparationFailure.MARKER_MISSING,
            False,
        ),
        (_vm(), ProviderLocator("opaque"), Deadline.after(0), VMTargetPreparationFailure.DEADLINE, True),
    ],
)
def test_pre_dispatch_refusals_do_not_borrow_or_dispatch(
    owned: tuple[Database, OperationOwner],
    vm: VMRow,
    locator: ProviderLocator | ProviderLocatorUnavailable,
    deadline: Deadline,
    failure: VMTargetPreparationFailure,
    expired: bool,
) -> None:
    database, owner = owned
    carrier = TranscriptCarrier(_success_payload())

    result = _prepare(owner, carrier, vm=vm, locator=locator, deadline=deadline)

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is failure
    assert result.deadline_exceeded is expired
    assert result.guest_result is None
    assert carrier.calls == 0
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.RESERVED  # type: ignore[union-attr]
    owner.close()


def test_malformed_persisted_marker_is_rejected_before_borrow_or_dispatch(
    owned: tuple[Database, OperationOwner],
) -> None:
    database, owner = owned
    carrier = TranscriptCarrier(_success_payload())

    with pytest.raises(ValidationError):
        _prepare(owner, carrier, vm=_vm(marker="not-a-marker"))

    assert carrier.calls == 0
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None
    assert claim.state is OperationClaimState.RESERVED
    owner.close()


@pytest.mark.parametrize(
    "scope",
    [
        OperationScope(OperationResourceKind.VM, "other-vm"),
        OperationScope(OperationResourceKind.PLATFORM_HOST, "box"),
    ],
)
def test_owner_must_cover_the_exact_vm_before_borrow_or_dispatch(
    owned: tuple[Database, OperationOwner],
    scope: OperationScope,
) -> None:
    database, vm_owner = owned
    other_owner = OperationOwner.acquire(database.operations, scope, "target-preparation")
    carrier = TranscriptCarrier(_success_payload())
    try:
        with pytest.raises(ValidationError):
            _prepare(other_owner, carrier)

        assert carrier.calls == 0
        claim = database.operations.inspect(scope)
        assert claim is not None
        assert claim.state is OperationClaimState.RESERVED
    finally:
        other_owner.close()
    vm_owner.close()


@pytest.mark.parametrize(
    ("payload", "failure"),
    [
        (
            encode_vm_guest_identity_failure("0" * 32, VMGuestIdentityFailure.MARKER_MISSING),
            VMTargetPreparationFailure.GUEST_REFUSED,
        ),
        (b"{}", VMTargetPreparationFailure.GUEST_OBSERVATION),
    ],
)
def test_typed_guest_refusal_and_invalid_observation_fail_without_retention(
    owned: tuple[Database, OperationOwner], payload: bytes, failure: VMTargetPreparationFailure
) -> None:
    _, owner = owned
    result = _prepare(owner, TranscriptCarrier(payload))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is failure
    assert result.guest_result is not None
    assert not result.requires_owner_retention
    owner.close()


def test_runtime_prerequisite_must_be_ready(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    carrier = TranscriptCarrier()

    def execute(invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        carrier.calls += 1
        assert isinstance(io.output, SinkOutput)
        nonce = _nonce(invocation)
        io.output.stdout.try_write(memoryview(f"AGW_RUNTIME_1:{nonce}:missing:-\n".encode("ascii")))
        return CarrierReport(Dispatch.SENT, ExitStatus(code=0))

    carrier.execute = execute  # type: ignore[method-assign]
    result = _prepare(owner, carrier)

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.RUNTIME_PREREQUISITE
    assert result.guest_result is not None
    owner.close()


def test_not_sent_is_settled_failed_without_owner_retention(owned: tuple[Database, OperationOwner]) -> None:
    database, owner = owned
    result = _prepare(owner, TranscriptCarrier(report=CarrierReport(Dispatch.NOT_SENT)))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.DISPATCH
    assert result.guest_result is not None
    assert not result.requires_owner_retention
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    owner.close()


@pytest.mark.parametrize(
    "report",
    [
        CarrierReport(Dispatch.UNKNOWN),
        CarrierReport(Dispatch.SENT, ExitStatus(code=1)),
    ],
)
def test_abnormal_or_unknown_attempt_requires_owner_retention(
    owned: tuple[Database, OperationOwner], report: CarrierReport
) -> None:
    database, owner = owned
    result = _prepare(owner, TranscriptCarrier(_success_payload(), report))

    assert result.status is VMTargetPreparationStatus.UNCERTAIN
    assert result.failure is VMTargetPreparationFailure.TERMINATION
    assert result.requires_owner_retention
    assert result.pending_remote_effects
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()


def test_carrier_exception_retains_owner_and_attaches_safe_control_fact(
    owned: tuple[Database, OperationOwner],
) -> None:
    database, owner = owned
    carrier = TranscriptCarrier(control=ControlStop())

    with pytest.raises(ControlStop) as raised:
        _prepare(owner, carrier)

    assert type(raised.value.__cause__) is VMTargetPreparationControlFact
    fact = raised.value.__cause__
    assert isinstance(fact, VMTargetPreparationControlFact)
    assert fact.preparation.status is VMTargetPreparationStatus.UNCERTAIN
    assert fact.preparation.pending_remote_effects
    assert fact.preparation.requires_owner_retention
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    with pytest.raises(StateError):
        owner.close()


def test_guest_evidence_survives_interrupted_settlement(
    owned: tuple[Database, OperationOwner],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, owner = owned

    def interrupt_settlement(
        self: object,
        dispatch: Dispatch,
        completion: ExitStatus | None,
    ) -> bool:
        del self, dispatch, completion
        raise ControlStop()

    monkeypatch.setattr(
        "agentworks.execution._fixed_helper_operation.BorrowedFixedHelperCarrier.settle",
        interrupt_settlement,
    )

    with pytest.raises(ControlStop) as raised:
        _prepare(owner, TranscriptCarrier(_success_payload()))

    fact = raised.value.__cause__
    assert isinstance(fact, VMTargetPreparationControlFact)
    assert fact.preparation.guest_result is not None
    assert fact.preparation.guest_result.observation is not None
    assert fact.preparation.status is VMTargetPreparationStatus.UNCERTAIN
    assert fact.preparation.requires_owner_retention
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None
    assert claim.state is OperationClaimState.POSSIBLE_DISPATCH
    with pytest.raises(StateError):
        owner.close()


def test_carrier_failure_overrides_a_forged_valid_guest_transcript(
    owned: tuple[Database, OperationOwner],
) -> None:
    _, owner = owned
    result = _prepare(
        owner,
        TranscriptCarrier(
            _success_payload(),
            CarrierReport(
                Dispatch.SENT,
                ExitStatus(code=0),
                stdout=CapturedOutput(complete=True, retention=Retention.DELIVERED),
                stderr=CapturedOutput(complete=True, retention=Retention.DELIVERED),
                failure=Failure.OUTPUT,
            ),
        ),
    )

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.CARRIER
    assert result.guest_result is not None
    assert result.guest_result.observation is not None
    assert not result.requires_owner_retention
    owner.close()


def test_marker_mismatch_is_a_typed_identity_failure(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    result = _prepare(owner, TranscriptCarrier(_success_payload(_OTHER_MARKER)))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.IDENTITY
    assert not result.requires_owner_retention
    owner.close()


def test_late_observation_is_not_prepared(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    result = _prepare(owner, TranscriptCarrier(_success_payload(), expire_on_return=True))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.DEADLINE
    assert result.deadline_exceeded
    owner.close()


def test_rejects_unbounded_deadline_before_borrow_or_dispatch(
    owned: tuple[Database, OperationOwner],
) -> None:
    database, owner = owned
    carrier = TranscriptCarrier(_success_payload())

    with pytest.raises(ValidationError):
        _prepare(owner, carrier, deadline=Deadline.after(None))

    assert carrier.calls == 0
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None
    assert claim.state is OperationClaimState.RESERVED
    owner.close()
