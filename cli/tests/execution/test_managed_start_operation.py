"""Core dispatch custody for one independent managed start."""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_start_operation as start_operation
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_job_request import ManagedJobRequest
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRecord,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._managed_start_operation import (
    ManagedStartControlFact,
    decode_managed_start_obligation,
    encode_managed_start_obligation,
    start_owned_managed_run,
)
from agentworks.execution._managed_start_protocol import (
    ManagedStartRequest,
    ManagedStartResult,
    decode_request,
    encode_result,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
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
from agentworks.operations import OperationBorrow, OperationOwner, _PreRegistrationClosingRefusal

RUN = ManagedRunIdentity("a" * 32)
OBLIGATION = "b" * 32
ROOT = IdentityExpectation(0, 0, (0,))


@pytest.fixture
def owned(tmp_path: Path) -> Generator[tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner]]:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    spec = ManagedRunSpec(
        ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", "v1:" + "c" * 64, "00000000-0000-4000-8000-000000000001"),
        IdentityExpectation(1001, 1001, (1001,)),
        ManagedShellIdentity(None, None),
        ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
        ManagedRunLifetime.INDEPENDENT,
    )
    record = repository.reserve(spec, output_policy=ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096), identity=RUN)
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "managed-start"
    )
    try:
        yield database, repository, record, owner
    finally:
        database.close()


def _request(record: ManagedRunRecord) -> ManagedJobRequest:
    return ManagedJobRequest(
        encode_managed_job_fact(ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)),
        "command",
        ("/usr/bin/true",),
        "/tmp",
        "capture",
        4096,
        (("TOKEN", "private-canary"),),
        b"",
        b"private-canary",
    )


class Carrier:
    def __init__(
        self,
        response: Callable[[ManagedStartRequest], bytes],
        *,
        code: int | None = 0,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.code = code
        self.error = error
        self.calls = 0
        self.validations = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self.validations += 1

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + self.response(request)
        io.output.stdout.try_write(memoryview(payload))
        channel = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(
            Dispatch.SENT,
            ExitStatus(code=self.code) if self.code is not None else None,
            stdout=channel,
            stderr=channel,
        )


def _records(request: ManagedStartRequest, *, receipt: bool) -> bytes:
    entries = [
        (FileRecordKind.RESULT, encode_result(ManagedStartResult(0, None, (FactName.LAUNCH,) if receipt else ())))
    ]
    if receipt:
        entries.append((FileRecordKind.DATA, request.job.launch))
    entries.append((FileRecordKind.FINISHED, b""))
    return b"".join(
        encode_file_record(request.nonce, FileRecord(index, kind, body)) for index, (kind, body) in enumerate(entries)
    )


def _start(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
    carrier: Carrier,
    *,
    request: ManagedJobRequest | None = None,
    owner: OperationOwner | None = None,
    deadline: Deadline | None = None,
    obligation_id: str = OBLIGATION,
):
    _, repository, record, existing_owner = owned
    return start_owned_managed_run(
        repository,
        record,
        carrier,
        request=request if request is not None else _request(record),
        plan=IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
        deadline=deadline if deadline is not None else Deadline.after(10),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        owner=owner if owner is not None else existing_owner,
        obligation_id=obligation_id,
    )


def test_confirmed_receipt_hands_off_to_resource_owned_run(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, record, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    outcome = _start(owned, carrier)
    assert carrier.calls == 1
    assert outcome.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    assert not outcome.requires_owner_retention
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED  # type: ignore[union-attr]
    rows = database.operations.list_lifecycle_obligations(owner.ownership)
    assert len(rows) == 1 and rows[0].state is LifecycleObligationState.RESOLVED
    assert rows[0].payload == encode_managed_start_obligation(RUN.run_id)
    assert decode_managed_start_obligation(rows[0].payload) == RUN
    assert b"private-canary" not in rows[0].payload
    assert b"private-canary" not in repr(outcome).encode()


def test_obligation_codec_refuses_noncanonical_or_extra_fields() -> None:
    for payload in (b"", b"{}", b'{"version":1,"run_id":"' + b"a" * 32 + b'"}', b"private-canary"):
        with pytest.raises(ValidationError):
            decode_managed_start_obligation(payload)


def test_wrong_vm_scope_refuses_before_borrow(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, record, _ = owned
    wrong = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "other-vm"), "managed-start"
    )
    carrier = Carrier(lambda request: _records(request, receipt=True))
    with pytest.raises(ValidationError):
        _start(owned, carrier, owner=wrong)
    assert not database.operations.list_lifecycle_obligations(wrong.ownership)
    assert repository.inspect(record.identity).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert carrier.calls == 0


def test_interrupted_registration_after_return_resolves_unused_row(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    original = OperationBorrow.install_dispatch_obligation

    def interrupted(self: OperationBorrow, *args: object, **kwargs: object) -> object:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("interrupted")

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", interrupted)
    with pytest.raises(RuntimeError) as caught:
        _start(owned, carrier)
    assert isinstance(caught.value.__cause__, ManagedStartControlFact)
    assert not caught.value.__cause__.outcome.requires_owner_retention
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert database.operations.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED
    assert carrier.calls == 0
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_owner_close_before_registration_releases_unused_borrow(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    original = OperationBorrow.install_dispatch_obligation

    def close_before_install(self: OperationBorrow, *args: object, **kwargs: object) -> object:
        with pytest.raises(StateError):
            owner.close()
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", close_before_install)
    with pytest.raises(_PreRegistrationClosingRefusal):
        _start(owned, carrier)
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
    assert carrier.calls == 0
    owner.close()


def test_owner_close_after_registration_before_arming_resolves_unused_row(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))

    def close_during_preflight(invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        carrier.validations += 1
        with pytest.raises(StateError):
            owner.close()

    monkeypatch.setattr(carrier, "validate", close_during_preflight)
    with pytest.raises(StateError) as caught:
        _start(owned, carrier)
    assert isinstance(caught.value.__cause__, ManagedStartControlFact)
    assert not caught.value.__cause__.outcome.requires_owner_retention
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert database.operations.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED
    assert carrier.calls == 0
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_invalid_obligation_id_releases_unused_borrow(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    with pytest.raises(ValueError):
        _start(owned, carrier, obligation_id="invalid")
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
    assert carrier.calls == 0
    owner.close()


def test_commit_uncertain_arming_hands_off_actual_possible_effect(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    owner_repository = owner._repository  # noqa: SLF001
    original = owner_repository.mark_lifecycle_obligation_possible_effect

    def commit_then_interrupt(*args: object, **kwargs: object) -> object:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(owner_repository, "mark_lifecycle_obligation_possible_effect", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        _start(owned, carrier)
    assert isinstance(caught.value.__cause__, ManagedStartControlFact)
    assert caught.value.__cause__.outcome.requires_owner_retention
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 0
    with pytest.raises(StateError):
        owner.close()


def test_interrupted_admission_keeps_possible_effect(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    original = OperationBorrow.arm_dispatch_obligation

    def interrupted(self: OperationBorrow) -> None:
        original(self)
        raise RuntimeError("interrupted")

    monkeypatch.setattr(OperationBorrow, "arm_dispatch_obligation", interrupted)
    with pytest.raises(RuntimeError) as caught:
        _start(owned, carrier)
    assert isinstance(caught.value.__cause__, ManagedStartControlFact)
    assert caught.value.__cause__.outcome.requires_owner_retention
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 0


def test_outcome_allocation_failure_keeps_confirmed_dispatch_custody(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    original = start_operation.ManagedStartOutcome
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> start_operation.ManagedStartOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("allocation failed")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(start_operation, "ManagedStartOutcome", fail_once)
    with pytest.raises(RuntimeError) as caught:
        _start(owned, carrier)
    assert isinstance(caught.value.__cause__, ManagedStartControlFact)
    assert caught.value.__cause__.outcome.requires_owner_retention
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 1


def test_deadline_after_admission_hands_off_without_carrier_attempt(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    deadline = Deadline.after(10)
    original = OperationBorrow.arm_dispatch_obligation

    def expire_after_admission(self: OperationBorrow) -> None:
        original(self)
        object.__setattr__(deadline, "expires_at", 0.0)

    monkeypatch.setattr(OperationBorrow, "arm_dispatch_obligation", expire_after_admission)
    outcome = _start(owned, carrier, deadline=deadline)
    assert outcome.deadline_exceeded
    assert outcome.requires_owner_retention
    assert not outcome.pending_remote_effects
    assert outcome.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 0


def test_settled_carrier_without_receipt_retains_effect(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=False))
    outcome = _start(owned, carrier)
    assert outcome.requires_owner_retention
    assert outcome.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 1


def test_unfinished_carrier_retains_outstanding_attempt(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=False), code=None)
    outcome = _start(owned, carrier)
    assert outcome.requires_owner_retention
    assert outcome.pending_remote_effects
    assert outcome.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 1


def test_preflight_refusal_leaves_reservation(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, record, owner = owned
    carrier = Carrier(lambda request: _records(request, receipt=True))
    bad = _request(record)
    bad = ManagedJobRequest(
        b"bad",
        bad.kind,
        bad.argv,
        bad.cwd,
        bad.output_mode,
        bad.capture_prefix_bytes,
        bad.environment,
        bad.source,
        bad.stdin,
    )
    with pytest.raises(ValidationError):
        _start(owned, carrier, request=bad)
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.RESERVED  # type: ignore[union-attr]
    assert database.operations.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED
    assert carrier.calls == 0


def test_escaping_carrier_retains_outstanding_attempt(
    owned: tuple[Database, ManagedRunRepository, ManagedRunRecord, OperationOwner],
) -> None:
    database, repository, _, owner = owned
    carrier = Carrier(lambda request: b"", error=RuntimeError("private-canary"))
    with pytest.raises(RuntimeError) as caught:
        _start(owned, carrier)
    assert isinstance(caught.value.__cause__, ManagedStartControlFact)
    assert caught.value.__cause__.outcome.requires_owner_retention
    assert b"private-canary" not in repr(caught.value.__cause__.outcome).encode()
    assert repository.inspect(RUN).launch_state is ManagedLaunchState.POSSIBLE_DISPATCH  # type: ignore[union-attr]
    assert (
        database.operations.list_lifecycle_obligations(owner.ownership)[0].state
        is LifecycleObligationState.POSSIBLE_EFFECT
    )
    assert carrier.calls == 1
