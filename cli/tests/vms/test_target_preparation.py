"""Owned managed VM target preparation behavior."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Event, Thread
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable, VMPlatform
from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope, VMRow
from agentworks.db.operations import OperationRepository
from agentworks.errors import StateError, ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity import VMGuestIdentityObservationResult, observe_vm_guest_identity
from agentworks.execution._vm_guest_identity_protocol import (
    VMGuestIdentity,
    VMGuestIdentityFailure,
    encode_vm_guest_identity_failure,
    encode_vm_guest_identity_success,
)
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import (
    CapturedOutput,
    Carrier,
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
from agentworks.operations import OperationBorrow, OperationOwner, release_borrow_after_custody
from agentworks.vms.target_identity import compose_managed_vm_target_identity
from agentworks.vms.target_preparation import (
    SelectedPlatformVMTargetPreparation,
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationFailure,
    VMTargetPreparationStatus,
    prepare_managed_vm_target,
    prepare_managed_vm_target_from_platform,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
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

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
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


def _platform(carrier: TranscriptCarrier) -> Mock:
    platform = Mock(spec=VMPlatform)
    platform.site_name = "local"
    platform.observe_provider_locator.return_value = ProviderLocator("opaque")
    platform.resolve_native_execution_binding.return_value = _binding(carrier)
    return platform


def _compose(
    owner: OperationOwner,
    platform: Mock,
    *,
    vm: VMRow | None = None,
    deadline: Deadline | None = None,
    ctx: object = None,
    config: object = None,
):
    return prepare_managed_vm_target_from_platform(
        vm or _vm(), platform, ctx, deadline=deadline or Deadline.after(10), owner=owner, config=config
    )


def _watch_custody(monkeypatch: pytest.MonkeyPatch) -> tuple[list[OperationBorrow], list[OperationBorrow]]:
    borrows: list[OperationBorrow] = []
    releases: list[OperationBorrow] = []
    original_borrow = OperationOwner.borrow

    def borrow(self: OperationOwner) -> OperationBorrow:
        result = original_borrow(self)
        borrows.append(result)
        return result

    def release(result: OperationBorrow) -> None:
        releases.append(result)
        release_borrow_after_custody(result)

    monkeypatch.setattr(OperationOwner, "borrow", borrow)
    monkeypatch.setattr("agentworks.vms.target_preparation.release_borrow_after_custody", release)
    return borrows, releases


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
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
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
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
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
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_not_sent_is_settled_failed_without_owner_retention(owned: tuple[Database, OperationOwner]) -> None:
    database, owner = owned
    result = _prepare(owner, TranscriptCarrier(report=CarrierReport(Dispatch.NOT_SENT)))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.DISPATCH
    assert result.guest_result is not None
    assert not result.requires_owner_retention
    assert database.operations.inspect(owner.ownership.scope).state is OperationClaimState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
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
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_marker_mismatch_is_a_typed_identity_failure(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    result = _prepare(owner, TranscriptCarrier(_success_payload(_OTHER_MARKER)))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.IDENTITY
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_late_observation_is_not_prepared(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    result = _prepare(owner, TranscriptCarrier(_success_payload(), expire_on_return=True))

    assert result.status is VMTargetPreparationStatus.FAILED
    assert result.failure is VMTargetPreparationFailure.DEADLINE
    assert result.deadline_exceeded
    assert not result.requires_owner_retention
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
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


@pytest.mark.parametrize("reason", ["wrong_owner", "missing_marker", "malformed_marker", "expired"])
def test_platform_preflight_refuses_before_any_io_or_borrow(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    database, owner = owned
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    vm = _vm(marker=None if reason == "missing_marker" else "invalid" if reason == "malformed_marker" else _MARKER)
    deadline = Deadline.after(0 if reason == "expired" else 10)
    selected_owner = owner
    if reason == "wrong_owner":
        selected_owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "other"), "target-preparation"
        )
    borrow = Mock(wraps=OperationOwner.borrow)
    monkeypatch.setattr(OperationOwner, "borrow", borrow)
    try:
        if reason in {"wrong_owner", "malformed_marker"}:
            with pytest.raises(ValidationError):
                _compose(selected_owner, platform, vm=vm, deadline=deadline)
        else:
            result = _compose(selected_owner, platform, vm=vm, deadline=deadline)
            assert result.preparation.failure is (
                VMTargetPreparationFailure.DEADLINE
                if reason == "expired"
                else VMTargetPreparationFailure.MARKER_MISSING
            )
            assert result.binding is None
        platform.observe_provider_locator.assert_not_called()
        platform.resolve_native_execution_binding.assert_not_called()
        assert carrier.calls == 0
        borrow.assert_not_called()
    finally:
        if selected_owner is not owner:
            selected_owner.close()
        owner.close()


def test_platform_locator_unavailable_skips_binding_and_releases_borrow(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    platform.observe_provider_locator.return_value = ProviderLocatorUnavailable()

    result = _compose(owner, platform)

    assert result.preparation.failure is VMTargetPreparationFailure.LOCATOR_UNAVAILABLE
    assert result.binding is None
    platform.resolve_native_execution_binding.assert_not_called()
    assert carrier.calls == 0
    assert len(borrows) == 1 and releases == borrows
    owner.close()


@pytest.mark.parametrize("bad_locator", [None, object(), "opaque", object.__new__(ProviderLocator)])
def test_invalid_platform_locator_shape_refuses_before_binding(
    owned: tuple[Database, OperationOwner], bad_locator: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    platform = _platform(TranscriptCarrier(_success_payload()))
    platform.observe_provider_locator.return_value = bad_locator
    with pytest.raises(ValidationError):
        _compose(owner, platform)
    platform.resolve_native_execution_binding.assert_not_called()
    assert len(borrows) == 1 and releases == borrows
    owner.close()


def test_forged_locator_and_binding_are_revalidated_at_plugin_boundary(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    locator = ProviderLocator("opaque")
    object.__setattr__(locator, "token", "")
    platform.observe_provider_locator.return_value = locator
    with pytest.raises(ValidationError):
        _compose(owner, platform)
    platform.resolve_native_execution_binding.assert_not_called()

    platform.observe_provider_locator.return_value = ProviderLocator("opaque")
    binding = _binding(carrier)
    object.__setattr__(binding, "delivery_account", "")
    platform.resolve_native_execution_binding.return_value = binding
    with pytest.raises(ValidationError):
        _compose(owner, platform)

    runtime = _runtime()
    object.__setattr__(runtime, "explicit_path", "relative")
    platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(carrier, "admin", runtime)
    with pytest.raises(ValidationError):
        _compose(owner, platform)
    assert carrier.calls == 0
    assert len(borrows) == 3 and releases == borrows
    owner.close()


@pytest.mark.parametrize("bad_binding", [None, object(), "binding", object.__new__(NativeExecutionBinding)])
def test_invalid_platform_binding_shape_refuses_before_guest(
    owned: tuple[Database, OperationOwner], bad_binding: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    platform.resolve_native_execution_binding.return_value = bad_binding
    with pytest.raises(ValidationError):
        _compose(owner, platform)
    assert carrier.calls == 0
    assert len(borrows) == 1 and releases == borrows
    owner.close()


def test_invalid_plugin_carrier_releases_borrow_before_guest(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    platform = _platform(TranscriptCarrier(_success_payload()))
    platform.resolve_native_execution_binding.return_value = NativeExecutionBinding(object(), "admin", _runtime())
    with pytest.raises(ValidationError):
        _compose(owner, platform)
    assert len(borrows) == 1 and releases == borrows
    owner.close()


def test_selected_platform_must_be_bound_to_vm_site(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    platform = _platform(TranscriptCarrier(_success_payload()))
    platform.site_name = "other"
    with pytest.raises(ValidationError):
        _compose(owner, platform)
    platform.observe_provider_locator.assert_not_called()
    owner.close()


@pytest.mark.parametrize("stage", ["locator", "binding"])
def test_platform_exception_releases_unused_borrow(
    owned: tuple[Database, OperationOwner], stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    getattr(
        platform, "observe_provider_locator" if stage == "locator" else "resolve_native_execution_binding"
    ).side_effect = ControlStop()
    with pytest.raises(ControlStop):
        _compose(owner, platform)
    assert len(borrows) == 1 and releases == borrows
    assert carrier.calls == 0
    owner.close()


def test_closed_owner_refuses_before_platform_io(owned: tuple[Database, OperationOwner]) -> None:
    _, owner = owned
    owner.close()
    platform = _platform(TranscriptCarrier(_success_payload()))

    with pytest.raises(StateError):
        _compose(owner, platform)

    platform.observe_provider_locator.assert_not_called()
    platform.resolve_native_execution_binding.assert_not_called()


def test_owner_closure_during_blocked_locator_refuses_dispatch_and_releases_borrow(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    entered = Event()
    resume = Event()
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    escaped: list[BaseException] = []

    def blocked_locator(*args: object, **kwargs: object) -> ProviderLocator:
        del args, kwargs
        entered.set()
        assert resume.wait(5)
        return ProviderLocator("opaque")

    def prepare() -> None:
        try:
            _compose(owner, platform)
        except BaseException as error:
            escaped.append(error)

    platform.observe_provider_locator.side_effect = blocked_locator
    worker = Thread(target=prepare)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(StateError):
            owner.close()
    finally:
        resume.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(escaped) == 1
    assert isinstance(escaped[0], StateError)
    fact = escaped[0].__cause__
    assert isinstance(fact, VMTargetPreparationControlFact)
    assert not fact.preparation.requires_owner_retention
    assert carrier.calls == 0
    assert len(borrows) == 1 and releases == borrows
    owner.close()


@pytest.mark.parametrize("stage", ["locator", "binding"])
def test_platform_late_result_refuses_before_next_stage(
    owned: tuple[Database, OperationOwner], stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    deadline = Deadline.after(10)

    def expire(*args: object, **kwargs: object) -> object:
        del args, kwargs
        object.__setattr__(deadline, "expires_at", 0.0)
        return ProviderLocator("opaque") if stage == "locator" else _binding(carrier)

    getattr(
        platform, "observe_provider_locator" if stage == "locator" else "resolve_native_execution_binding"
    ).side_effect = expire
    result = _compose(owner, platform, deadline=deadline)
    assert result.preparation.failure is VMTargetPreparationFailure.DEADLINE
    assert result.preparation.deadline_exceeded
    assert result.binding is None
    if stage == "locator":
        platform.resolve_native_execution_binding.assert_not_called()
    assert carrier.calls == 0
    assert len(borrows) == 1 and releases == borrows
    owner.close()


def test_platform_composition_passes_exact_inputs_and_leaves_outer_owner_open(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    carrier = TranscriptCarrier(_success_payload(), deadlines=[])
    platform = _platform(carrier)
    ctx = object()
    config = object()
    deadline = Deadline.after(10)
    borrows, releases = _watch_custody(monkeypatch)
    probe_selection: RuntimeSelection | None = None
    probe_carrier: Carrier | None = None

    def observe(*args: object, **kwargs: object) -> ProviderLocator:
        del args, kwargs
        if platform.observe_provider_locator.call_count == 2:
            assert len(borrows) == 1
            assert releases == []
            with pytest.raises(StateError):
                owner.borrow()
        return ProviderLocator("opaque")

    def probe(
        selected_carrier: Carrier, *, runtime_selection: RuntimeSelection, deadline: Deadline
    ) -> VMGuestIdentityObservationResult:
        nonlocal probe_selection, probe_carrier
        probe_selection = runtime_selection
        probe_carrier = selected_carrier
        return observe_vm_guest_identity(selected_carrier, runtime_selection=runtime_selection, deadline=deadline)

    monkeypatch.setattr("agentworks.vms.target_preparation.observe_vm_guest_identity", probe)
    platform.observe_provider_locator.side_effect = observe
    result = _compose(owner, platform, ctx=ctx, config=config, deadline=deadline)

    assert result.preparation.status is VMTargetPreparationStatus.PREPARED
    assert result.preparation.target == compose_managed_vm_target_identity(
        _vm(), ProviderLocator("opaque"), VMGuestIdentity(_MARKER, _BOOT_ID)
    )
    assert result.binding is not None
    assert result.binding.carrier is carrier
    assert result.binding.delivery_account == "admin"
    assert result.binding.runtime_selection == _runtime()
    assert result.binding.runtime_selection is probe_selection
    assert probe_carrier is not None
    assert result.binding is not platform.resolve_native_execution_binding.return_value
    assert "binding" not in repr(result)
    assert carrier.calls == 1
    assert carrier.deadlines == [deadline]
    assert len(borrows) == 1 and releases == borrows
    assert platform.observe_provider_locator.call_count == 2
    assert platform.observe_provider_locator.call_args_list == [call(_vm(), ctx, deadline=deadline)] * 2
    platform.resolve_native_execution_binding.assert_called_once_with(_vm(), ctx, deadline=deadline, config=config)
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize("report", [CarrierReport(Dispatch.NOT_SENT), CarrierReport(Dispatch.UNKNOWN)])
def test_selected_platform_drops_binding_on_failed_or_uncertain_guest_attempt(
    owned: tuple[Database, OperationOwner], report: CarrierReport
) -> None:
    _, owner = owned
    platform = _platform(TranscriptCarrier(_success_payload(), report))

    result = _compose(owner, platform)

    assert result.preparation.status is not VMTargetPreparationStatus.PREPARED
    assert result.binding is None
    platform.observe_provider_locator.assert_called_once()
    if not result.preparation.requires_owner_retention:
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


@pytest.mark.parametrize("replacement_stage", ["binding", "probe"])
def test_second_locator_detects_cooperative_replacement_before_or_during_probe(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch, replacement_stage: str
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    current = ["first"]
    platform.observe_provider_locator.side_effect = lambda *args, **kwargs: ProviderLocator(current[0])
    if replacement_stage == "binding":

        def resolve(*args: object, **kwargs: object) -> NativeExecutionBinding:
            del args, kwargs
            current[0] = "second"
            return _binding(carrier)

        platform.resolve_native_execution_binding.side_effect = resolve
    else:
        original_execute = carrier.execute

        def execute(invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            result = original_execute(invocation, io=io, deadline=deadline)
            current[0] = "second"
            return result

        carrier.execute = execute  # type: ignore[method-assign]

    result = _compose(owner, platform)

    assert result.preparation.status is VMTargetPreparationStatus.FAILED
    assert result.preparation.failure is VMTargetPreparationFailure.LOCATOR_CHANGED
    assert result.preparation.guest_result is not None
    assert result.preparation.target is None
    assert result.binding is None
    assert platform.observe_provider_locator.call_count == 2
    assert carrier.calls == 1
    assert len(borrows) == 1 and releases == borrows
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize("confirmation", ["unavailable", "malformed", "late"])
def test_second_locator_refuses_unavailable_malformed_or_late_result(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch, confirmation: str
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    deadline = Deadline.after(10)

    def observe(*args: object, **kwargs: object) -> object:
        del args, kwargs
        if platform.observe_provider_locator.call_count == 1:
            return ProviderLocator("opaque")
        if confirmation == "late":
            object.__setattr__(deadline, "expires_at", 0.0)
            return ProviderLocator("opaque")
        if confirmation == "unavailable":
            return ProviderLocatorUnavailable()
        return "malformed"

    platform.observe_provider_locator.side_effect = observe
    if confirmation == "malformed":
        with pytest.raises(ValidationError):
            _compose(owner, platform, deadline=deadline)
    else:
        result = _compose(owner, platform, deadline=deadline)
        assert result.preparation.status is VMTargetPreparationStatus.FAILED
        assert result.preparation.failure is (
            VMTargetPreparationFailure.DEADLINE
            if confirmation == "late"
            else VMTargetPreparationFailure.LOCATOR_CHANGED
        )
        assert result.preparation.guest_result is not None
        assert result.preparation.target is None
        assert result.binding is None
        assert result.preparation.deadline_exceeded is (confirmation == "late")
    assert platform.observe_provider_locator.call_count == 2
    assert carrier.calls == 1
    assert len(borrows) == 1 and releases == borrows
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize("selected_platform", [False, True])
def test_repository_release_failure_suppresses_prepared_target(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch, selected_platform: bool
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    resolution_calls = 0

    def fail_resolution(*args: object, **kwargs: object) -> None:
        nonlocal resolution_calls
        del args, kwargs
        resolution_calls += 1
        raise StateError("injected obligation resolution failure")

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", fail_resolution)
    with pytest.raises(StateError) as raised:
        if selected_platform:
            _compose(owner, platform)
        else:
            _prepare(owner, carrier)

    fact = raised.value.__cause__
    assert isinstance(fact, VMTargetPreparationControlFact)
    assert fact.preparation.status is VMTargetPreparationStatus.UNCERTAIN
    assert fact.preparation.target is None
    assert fact.preparation.guest_result is not None
    assert fact.preparation.coordination_uncertain
    assert fact.preparation.requires_owner_retention
    assert not fact.preparation.pending_remote_effects
    assert carrier.calls == 1
    assert resolution_calls == 1
    assert len(borrows) == 1 and releases == borrows
    if selected_platform:
        assert platform.observe_provider_locator.call_count == 2
    with pytest.raises(StateError):
        owner.borrow()
    with pytest.raises(StateError):
        owner.close()


def test_confirmation_error_and_release_error_preserve_guest_and_control_chain(
    owned: tuple[Database, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner = owned
    borrows, releases = _watch_custody(monkeypatch)
    carrier = TranscriptCarrier(_success_payload())
    platform = _platform(carrier)
    platform.observe_provider_locator.side_effect = [ProviderLocator("opaque"), "malformed"]

    def fail_resolution(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise StateError("injected obligation resolution failure")

    monkeypatch.setattr(OperationRepository, "resolve_lifecycle_obligation", fail_resolution)
    with pytest.raises(StateError) as raised:
        _compose(owner, platform)

    fact = raised.value.__cause__
    assert isinstance(fact, VMTargetPreparationControlFact)
    assert fact.preparation.status is VMTargetPreparationStatus.UNCERTAIN
    assert fact.preparation.target is None
    assert fact.preparation.guest_result is not None
    assert fact.preparation.failure is VMTargetPreparationFailure.LOCATOR_CHANGED
    assert fact.preparation.coordination_uncertain
    assert fact.preparation.requires_owner_retention
    original = fact.__cause__
    assert isinstance(original, ValidationError)
    assert isinstance(original.__cause__, VMTargetPreparationControlFact)
    assert original.__cause__.preparation.guest_result is not None
    assert original.__cause__.preparation.target is None
    assert carrier.calls == 1
    assert len(borrows) == 1 and releases == borrows
    with pytest.raises(StateError):
        owner.borrow()


def test_selected_platform_result_rejects_binding_status_mismatch() -> None:
    failed = VMTargetPreparation(VMTargetPreparationStatus.FAILED, None, None)
    target = compose_managed_vm_target_identity(_vm(), ProviderLocator("opaque"), VMGuestIdentity(_MARKER, _BOOT_ID))
    prepared = VMTargetPreparation(VMTargetPreparationStatus.PREPARED, target, None)
    binding = _binding(TranscriptCarrier())

    with pytest.raises(ValidationError):
        SelectedPlatformVMTargetPreparation(failed, binding)
    with pytest.raises(ValidationError):
        SelectedPlatformVMTargetPreparation(prepared, None)


def test_preparation_value_rejects_inconsistent_status_and_retention() -> None:
    target = compose_managed_vm_target_identity(_vm(), ProviderLocator("opaque"), VMGuestIdentity(_MARKER, _BOOT_ID))
    prepared = VMTargetPreparation(VMTargetPreparationStatus.PREPARED, target, None)
    failed = VMTargetPreparation(VMTargetPreparationStatus.FAILED, None, None)
    uncertain = VMTargetPreparation(VMTargetPreparationStatus.UNCERTAIN, None, None, requires_owner_retention=True)

    invalid_cases: tuple[Callable[[], VMTargetPreparation], ...] = (
        lambda: replace(prepared, target=None),
        lambda: replace(prepared, failure=VMTargetPreparationFailure.IDENTITY),
        lambda: replace(prepared, deadline_exceeded=True),
        lambda: replace(prepared, pending_remote_effects=True),
        lambda: replace(prepared, coordination_uncertain=True),
        lambda: replace(prepared, requires_owner_retention=True),
        lambda: replace(failed, target=target),
        lambda: replace(failed, requires_owner_retention=True),
        lambda: replace(failed, pending_remote_effects=True),
        lambda: replace(failed, coordination_uncertain=True),
        lambda: replace(uncertain, requires_owner_retention=False),
    )
    for invalid in invalid_cases:
        with pytest.raises(ValidationError):
            invalid()
