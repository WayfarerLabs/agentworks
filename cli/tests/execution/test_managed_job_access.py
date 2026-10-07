"""Private bound managed-job composition and reservation boundaries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _fixed_helper_operation as fixed_operation
from agentworks.execution import _managed_job_access as access
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRepository,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._managed_start_protocol import ManagedStartRequest
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution._workload_shell import WorkloadShellObservationResult
from agentworks.execution._workload_shell_protocol import WorkloadShellResponse, encode_workload_shell_response
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.models import Command, Input, Output, Script, Shell
from agentworks.operations import OperationOwner

from .test_managed_start_operation import Carrier, _records

RUN = ManagedRunIdentity("e" * 32)
ROOT = IdentityExpectation(0, 0, (0,))
WORKLOAD = IdentityExpectation(1001, 1001, (1001,))
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)
TARGET = ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", "v1:" + "c" * 64, vm_guest_boot_id(GUEST))


def _call(
    repository: ManagedRunRepository,
    held_owner: OperationOwner,
    carrier: Carrier,
    *,
    invocation: Command | Script | None = None,
    **changes: object,
):
    options = {
        "target": TARGET,
        "workload_plan": IdentityPlan(WORKLOAD, IdentityMode.DEMOTE),
        "root_plan": IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
        "run_owner": ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
        "input": Input.eof(),
        "output": Output.capture(4096),
        "env": None,
        "cwd": None,
        "sensitive": False,
        "carrier": carrier,
        "runtime_selection": RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        "deadline": Deadline.after(10),
        "owner": held_owner,
        "obligation_id": "b" * 32,
        "identity": RUN,
        "guest": GUEST,
    }
    options.update(changes)
    return access.start_bound_managed_job(repository, invocation or Command(["/usr/bin/true"]), **options)


def test_start_binds_request_reservation_and_owned_custody(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    try:
        outcome = _call(repository, owner, carrier)
        assert outcome.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
        assert outcome.attempt is not None
        assert outcome.attempt.record.identity == RUN
        assert outcome.attempt.record.spec.workload == WORKLOAD
        assert repository.inspect(RUN) == outcome.attempt.record
        assert carrier.calls == 1
        assert not outcome.requires_owner_retention
    finally:
        database.close()


def test_bound_start_forwards_route_check_after_arming(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    observed: list[ManagedLaunchState] = []

    def check_route() -> None:
        row = repository.inspect(RUN)
        assert row is not None
        observed.append(row.launch_state)
        assert (
            database.operations.list_pending_lifecycle_obligations(owner.ownership)[0].state
            is LifecycleObligationState.POSSIBLE_EFFECT
        )
        assert carrier.calls == 0

    try:
        assert (
            _call(repository, owner, carrier, before_dispatch=check_route).launch_state
            is ManagedLaunchState.RECEIPT_CONFIRMED
        )
        assert observed == [ManagedLaunchState.RESERVED]
        assert carrier.calls == 1
    finally:
        database.close()


def test_invalid_request_and_carrier_refusal_leave_no_reservation(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    try:
        with pytest.raises(ValidationError):
            _call(repository, owner, carrier, env={"bad=name": "x"})
        with pytest.raises(ValidationError):
            _call(repository, owner, carrier, obligation_id="invalid")

        class UnsupportedCarrier(Carrier):
            def validate(self, invocation, *, io):
                raise ValidationError("unsupported direct envelope")

        unsupported = UnsupportedCarrier(lambda request: _records(request, receipt=True))
        with pytest.raises(ValidationError):
            _call(repository, owner, unsupported)
        assert repository.inspect(RUN) is None
        assert carrier.calls == unsupported.calls == 0
    finally:
        owner.close()
        database.close()


@pytest.mark.parametrize("changed", [replace(GUEST, boot_id="00000000-0000-4000-8000-000000000002"), object()])
def test_guest_boot_mismatch_refuses_before_reservation(tmp_path: Path, changed: object) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    try:
        with pytest.raises(ValidationError):
            _call(repository, owner, carrier, guest=changed)
        assert repository.inspect(RUN) is None
        assert carrier.calls == carrier.validations == 0
    finally:
        owner.close()
        database.close()


def test_deadline_expiring_during_carrier_validation_leaves_no_reservation(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    deadline = Deadline.after(10)

    class ExpiringCarrier(Carrier):
        def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            super().validate(invocation, io=io)
            object.__setattr__(deadline, "expires_at", 0.0)

    carrier = ExpiringCarrier(lambda request: _records(request, receipt=True))
    try:
        with pytest.raises(ValidationError):
            _call(repository, owner, carrier, deadline=deadline)
        assert repository.inspect(RUN) is None
        assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
        assert carrier.calls == 0
    finally:
        owner.close()
        database.close()


def test_deadline_expiring_after_reservation_keeps_one_shot_tombstone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    deadline = Deadline.after(10)
    carrier = Carrier(lambda request: _records(request, receipt=True))
    original_reserve = repository.reserve

    def reserve_then_expire(*args: object, **kwargs: object):
        record = original_reserve(*args, **kwargs)
        object.__setattr__(deadline, "expires_at", 0.0)
        return record

    monkeypatch.setattr(repository, "reserve", reserve_then_expire)
    try:
        with pytest.raises(ValidationError):
            _call(repository, owner, carrier, deadline=deadline)
        reserved = repository.inspect(RUN)
        assert reserved is not None
        assert reserved.launch_state is ManagedLaunchState.RESERVED
        assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
        assert carrier.calls == 0
    finally:
        database.close()


def test_owner_borrow_refusal_after_reservation_keeps_known_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))

    def refuse_borrow() -> None:
        raise StateError("owner cannot lend custody")

    monkeypatch.setattr(owner, "borrow", refuse_borrow)
    try:
        with pytest.raises(StateError):
            _call(repository, owner, carrier)
        reserved = repository.inspect(RUN)
        assert reserved is not None
        assert reserved.identity == RUN
        assert reserved.launch_state is ManagedLaunchState.RESERVED
        assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
        assert carrier.calls == 0
    finally:
        database.close()


def test_missing_receipt_keeps_uncertain_start_custody(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=False))
    try:
        outcome = _call(repository, owner, carrier)
        assert outcome.launch_state is ManagedLaunchState.POSSIBLE_DISPATCH
        assert outcome.requires_owner_retention
        assert repository.inspect(RUN) is not None
        assert carrier.calls == 1
    finally:
        database.close()


@pytest.mark.parametrize("wrong", ["owner", "target"])
def test_wrong_binding_refuses_before_reservation(tmp_path: Path, wrong: str) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    second_owner = None
    changed: object
    try:
        if wrong == "owner":
            second_owner = OperationOwner.acquire(
                database.operations, OperationScope(OperationResourceKind.VM, "vm-two"), "start"
            )
            changed = second_owner
        elif wrong == "target":
            changed = replace(TARGET, name="vm-two")
        with pytest.raises(ValidationError):
            _call(repository, owner, carrier, **{wrong: changed})
        assert repository.inspect(RUN) is None
        assert carrier.calls == 0
    finally:
        owner.close()
        if second_owner is not None:
            second_owner.close()
        database.close()


def test_sensitive_script_suppresses_output_policy(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    secret = "private-source-canary"
    try:
        outcome = access.start_bound_managed_job(
            repository,
            Script(secret, Shell.SH),
            target=TARGET,
            workload_plan=IdentityPlan(WORKLOAD, IdentityMode.DEMOTE),
            root_plan=IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
            run_owner=ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
            input=Input.sensitive(b"private-input-canary"),
            output=Output.capture(4096),
            env=None,
            cwd=None,
            sensitive=False,
            carrier=carrier,
            runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
            deadline=Deadline.after(10),
            owner=owner,
            obligation_id="b" * 32,
            identity=RUN,
            guest=GUEST,
        )
        assert outcome.attempt is not None
        assert outcome.attempt.record.output_policy.mode is ManagedOutputMode.SENSITIVITY_SUPPRESSED
        assert secret not in repr(outcome)
    finally:
        database.close()


def test_unresolved_user_default_shell_refuses_without_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    observation = WorkloadShellObservationResult(
        Dispatch.NOT_SENT,
        None,
        None,
        Failure.DEADLINE,
        RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
        None,
    )
    monkeypatch.setattr(access, "observe_workload_shell", lambda *_args, **_kwargs: observation)
    try:
        with pytest.raises(access.ManagedJobShellRefusal) as caught:
            _call(
                repository, owner, carrier, invocation=Script("echo hello", Shell.USER_DEFAULT), output=Output.discard()
            )
        assert caught.value.fact.observation == observation
        assert repository.inspect(RUN) is None
        assert carrier.calls == 0
    finally:
        owner.close()
        database.close()


def test_shell_release_interrupt_preserves_original_control_and_safe_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    observation = WorkloadShellObservationResult(
        Dispatch.NOT_SENT,
        None,
        None,
        Failure.DEADLINE,
        RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
        None,
    )
    control = KeyboardInterrupt("release interrupted")

    def fail_release(*_args: object, **_kwargs: object) -> None:
        raise control

    monkeypatch.setattr(access, "observe_workload_shell", lambda *_args, **_kwargs: observation)
    monkeypatch.setattr(fixed_operation, "release_borrow_after_custody", fail_release)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            _call(repository, owner, carrier, invocation=Script("echo hello", Shell.USER_DEFAULT))
        assert caught.value is control
        assert isinstance(control.__cause__, access.ManagedJobShellRefusal)
        assert control.__cause__.fact.observation == observation
        assert control.__cause__.fact.coordination_uncertain
        assert control.__cause__.fact.requires_owner_retention
        assert repository.inspect(RUN) is None
        assert carrier.calls == 0
    finally:
        database.close()


@pytest.mark.parametrize(
    "input,output,sensitive,mode,prefix",
    (
        (Input.bytes(b"literal input"), Output.capture(7), False, ManagedOutputMode.CAPTURE, 7),
        (Input.bytes(b"literal input"), Output.discard(), False, ManagedOutputMode.DISCARD, None),
        (Input.sensitive(b"literal input"), Output.capture(7), False, ManagedOutputMode.SENSITIVITY_SUPPRESSED, None),
        (Input.bytes(b"literal input"), Output.discard(), True, ManagedOutputMode.SENSITIVITY_SUPPRESSED, None),
    ),
)
def test_user_default_start_freezes_body_before_shell_observation(
    tmp_path: Path,
    input: Input,
    output: Output,
    sensitive: bool,
    mode: ManagedOutputMode,
    prefix: int | None,
) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")

    class ProbeEnvironment(dict[str, str]):
        traversals = 0

        def items(self):
            self.traversals += 1
            return super().items()

    environment = ProbeEnvironment(LANG="C")
    saved: list[ManagedStartRequest] = []

    def start_response(request: ManagedStartRequest) -> bytes:
        saved.append(request)
        return _records(request, receipt=True)

    class DualCarrier(Carrier):
        def execute(
            self,
            invocation: PreparedInvocation,
            *,
            io: CarrierIO,
            deadline: Deadline,
            custody: LocalDeliveryCustody | None = None,
        ) -> CarrierReport:
            assert isinstance(io.input, FiniteInput)
            if io.input.data.startswith(FIXED_BUNDLE.prefix):
                return super().execute(invocation, io=io, deadline=deadline, custody=custody)
            self.calls += 1
            environment["LANG"] = "changed"
            assert isinstance(io.output, SinkOutput)
            nonce = invocation.argv[invocation.argv.index("agentworks-runtime-prerequisite") + 1]
            payload = f"AGW_RUNTIME_1:{nonce}:ready:0\n".encode()
            payload += encode_workload_shell_response(nonce, WorkloadShellResponse(shell="/usr/bin/bash"))
            io.output.stdout.try_write(memoryview(payload))
            channel = CapturedOutput(complete=True, retention=Retention.DELIVERED)
            return CarrierReport(Dispatch.SENT, ExitStatus(code=0), stdout=channel, stderr=channel)

    carrier = DualCarrier(start_response)
    try:
        outcome = _call(
            repository,
            owner,
            carrier,
            invocation=Script("read value", Shell.USER_DEFAULT, login=True),
            env=environment,
            input=input,
            output=output,
            sensitive=sensitive,
            cwd="/tmp",
        )
        assert outcome.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
        assert outcome.attempt is not None
        assert outcome.attempt.record.spec.shell.requested is Shell.USER_DEFAULT
        assert outcome.attempt.record.spec.shell.resolved_executable == "/usr/bin/bash"
        assert carrier.calls == 2
        assert (environment.traversals, saved[0].job.environment) == (1, (("LANG", "C"),))
        assert environment["LANG"] == "changed"
        request = saved[0].job
        assert request.source == b"read value" and request.stdin == input.data and request.cwd == "/tmp"
        record = outcome.attempt.record
        assert record.spec.lifetime is ManagedRunLifetime.INDEPENDENT and record.spec.shell.login
        assert request.launch == encode_managed_job_fact(
            ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)
        )
        assert request.output_mode == mode.value and request.capture_prefix_bytes == prefix
        assert record.output_policy.mode is mode and record.output_policy.capture_prefix_bytes == prefix
    finally:
        database.close()


@pytest.mark.parametrize("invalid", ("env", "cwd", "input", "output", "source"))
def test_invalid_user_default_request_refuses_before_shell_probe(tmp_path: Path, invalid: str) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True))
    cases: dict[str, dict[str, object]] = {
        "env": {"env": {"bad=name": "x"}},
        "cwd": {"cwd": "relative"},
        "input": {"input": b"untyped"},
        "output": {"output": Output.capture(2**30)},
        "source": {"invocation": Script("\ud800", Shell.USER_DEFAULT)},
    }
    changes = cases[invalid]
    try:
        with pytest.raises(ValidationError):
            _call(
                repository,
                owner,
                carrier,
                **{"invocation": Script("echo hello", Shell.USER_DEFAULT), **changes},
            )
        assert carrier.calls == 0
        assert repository.inspect(RUN) is None
        assert owner.list_pending_lifecycle_obligations() == ()
    finally:
        owner.close()
        database.close()


def test_shell_probe_control_flow_keeps_uncertain_custody(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "start")
    carrier = Carrier(lambda request: _records(request, receipt=True), error=RuntimeError("lost shell reply"))
    try:
        with pytest.raises(RuntimeError) as caught:
            _call(repository, owner, carrier, invocation=Script("echo hello", Shell.USER_DEFAULT))
        assert isinstance(caught.value.__cause__, access.ManagedJobShellRefusal)
        assert caught.value.__cause__.fact.requires_owner_retention
        assert repository.inspect(RUN) is None
        assert carrier.calls == 1
    finally:
        database.close()
