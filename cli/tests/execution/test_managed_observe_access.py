"""Exact persisted-run admission and custody for private managed observation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _fixed_helper_operation as fixed_operation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_protocol import StreamDisposition, decode_managed_job_fact
from agentworks.execution._managed_job_store import FactName, Stream
from agentworks.execution._managed_observation_exchange import ManagedObservationState
from agentworks.execution._managed_observation_protocol import ManagedResultControl
from agentworks.execution._managed_observe_access import (
    ManagedObserveControlFact,
    observe_bound_managed_run,
    read_bound_managed_output,
)
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRepository,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.operations import OperationOwner

from .test_managed_observation import ScriptedCarrier, _fact, _records
from .test_managed_observation import _launch as _wire_launch

RUN = ManagedRunIdentity("a" * 32)
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)
TARGET = ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", "v1:" + "c" * 64, vm_guest_boot_id(GUEST))
ROOT_PLAN = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)


def _reserved(
    tmp_path: Path, output_policy: ManagedOutputPolicy | None = None
) -> tuple[Database, ManagedRunRepository, OperationOwner]:
    database = Database(tmp_path / "state.db")
    repository = ManagedRunRepository(database)
    repository.reserve(
        ManagedRunSpec(
            TARGET,
            IdentityExpectation(1001, 1001, (1001,)),
            ManagedShellIdentity(None, None),
            ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
            ManagedRunLifetime.INDEPENDENT,
        ),
        output_policy=output_policy or ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096),
        identity=RUN,
    )
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "observe")
    return database, repository, owner


def _options(owner: OperationOwner, carrier: ScriptedCarrier, **changes: object) -> dict[str, object]:
    options = {
        "target": TARGET,
        "guest": GUEST,
        "root_plan": ROOT_PLAN,
        "carrier": carrier,
        "runtime_selection": RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3"),
        "deadline": Deadline.after(10),
        "owner": owner,
    }
    options.update(changes)
    return options


def _observe(repository: ManagedRunRepository, owner: OperationOwner, carrier: ScriptedCarrier, **changes: object):  # type: ignore[no-untyped-def]
    return observe_bound_managed_run(repository, RUN, **_options(owner, carrier, **changes))  # type: ignore[arg-type]


def _read(repository: ManagedRunRepository, owner: OperationOwner, carrier: ScriptedCarrier, **changes: object):  # type: ignore[no-untyped-def]
    options = _options(owner, carrier, stream=Stream.STDOUT)
    options.update(changes)
    return read_bound_managed_output(
        repository,
        RUN,
        **options,  # type: ignore[arg-type]
    )


def _output_reply(request, *, disposition: str, content: bytes = b"", delivered: bytes | None = None):  # type: ignore[no-untyped-def]
    assert request.stream is not None
    end_name = FactName.STDOUT_END if request.stream is Stream.STDOUT else FactName.STDERR_END
    end = _fact(end_name, request.expected_launch, disposition=disposition, content=content)
    return _records(
        request.nonce,
        ManagedResultControl((FactName.LAUNCH, end_name)),
        (request.expected_launch, end),
        content if delivered is None and disposition in ("complete-capture", "truncated-capture") else delivered or b"",
    )


def test_observe_uses_exact_persisted_launch_and_does_not_change_row(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    record = repository.inspect(RUN)
    assert record is not None

    def response(request):  # type: ignore[no-untyped-def]
        receipt = decode_managed_job_fact(request.expected_launch)
        assert isinstance(receipt, ManagedRunReceipt)
        assert receipt.identity == record.identity
        assert receipt.spec == record.spec
        return _records(request.nonce, ManagedResultControl((FactName.LAUNCH,)), (request.expected_launch,))

    carrier = ScriptedCarrier(response)
    try:
        outcome = _observe(repository, owner, carrier)
        assert outcome.candidate is not None
        assert outcome.candidate.observation is not None
        assert outcome.candidate.observation.state is ManagedObservationState.OBSERVED
        assert [name for name, _ in outcome.candidate.observation.facts] == [FactName.LAUNCH]
        assert not outcome.requires_owner_retention
        assert carrier.calls == 1
        assert repository.inspect(RUN) == record
        assert record.launch_state is ManagedLaunchState.RESERVED
    finally:
        database.close()


@pytest.mark.parametrize("case", ["absent", "target", "boot", "operation", "platform"])
def test_refuses_wrong_row_or_target_before_borrow_and_carrier(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(lambda request: b"")
    changes: dict[str, object] = {}
    borrow_calls = 0
    original_borrow = owner.borrow

    def borrow_spy():  # type: ignore[no-untyped-def]
        nonlocal borrow_calls
        borrow_calls += 1
        return original_borrow()

    monkeypatch.setattr(owner, "borrow", borrow_spy)
    try:
        if case == "absent":
            repository._connection.execute("DELETE FROM execution_runs WHERE run_id = ?", (RUN.run_id,))  # noqa: SLF001
        elif case == "target":
            changes["target"] = replace(TARGET, incarnation="v1:" + "b" * 64)
        elif case == "boot":
            changes["guest"] = replace(GUEST, init_start_ticks=1235)
        elif case == "operation":
            repository._connection.execute(  # noqa: SLF001
                "UPDATE execution_runs SET owner_kind = ?, owner_id = ?, lifetime = ? WHERE run_id = ?",
                ("operation", RUN.run_id, "operation", RUN.run_id),
            )
        else:
            repository._connection.execute(  # noqa: SLF001
                "UPDATE execution_runs SET target_kind = ? WHERE run_id = ?", ("platform-host", RUN.run_id)
            )
        with pytest.raises((ValidationError, StateError)):
            _observe(repository, owner, carrier, **changes)
        assert carrier.calls == 0
        assert borrow_calls == 0
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
        borrow = owner.borrow()
        borrow.close()
    finally:
        database.close()


def test_ambiguous_dispatch_retains_owner_without_replay(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    row = repository.inspect(RUN)
    carrier = ScriptedCarrier(lambda request: b"", dispatch=Dispatch.UNKNOWN, code=None)
    try:
        outcome = _observe(repository, owner, carrier)
        assert outcome.candidate is not None
        assert outcome.pending_remote_effects
        assert outcome.requires_owner_retention
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()


@pytest.mark.parametrize("read", [False, True])
def test_carrier_base_exception_preserves_original_with_custody_fact(tmp_path: Path, read: bool) -> None:
    database, repository, owner = _reserved(tmp_path)
    row = repository.inspect(RUN)
    interrupted = KeyboardInterrupt()

    class InterruptedCarrier(ScriptedCarrier):
        def execute(self, invocation, *, io, deadline):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise interrupted

    carrier = InterruptedCarrier(lambda request: b"")
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            if read:
                _read(repository, owner, carrier)
            else:
                _observe(repository, owner, carrier)
        assert raised.value is interrupted
        assert isinstance(raised.value.__cause__, ManagedObserveControlFact)
        assert raised.value.__cause__.outcome.requires_owner_retention
        assert raised.value.__cause__.outcome.pending_remote_effects
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
    finally:
        database.close()


def test_release_base_exception_preserves_original_with_custody_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _reserved(tmp_path)
    interrupted = KeyboardInterrupt("release interrupted")
    carrier = ScriptedCarrier(
        lambda request: _records(request.nonce, ManagedResultControl((FactName.LAUNCH,)), (request.expected_launch,))
    )

    def fail_release(*_args: object, **_kwargs: object) -> None:
        raise interrupted

    monkeypatch.setattr(fixed_operation, "release_borrow_after_custody", fail_release)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _observe(repository, owner, carrier)
        assert raised.value is interrupted
        assert isinstance(interrupted.__cause__, ManagedObserveControlFact)
        assert interrupted.__cause__.outcome.candidate is not None
        assert interrupted.__cause__.outcome.coordination_uncertain
        assert interrupted.__cause__.outcome.requires_owner_retention
        assert carrier.calls == 1
    finally:
        database.close()


@pytest.mark.parametrize("stream", [Stream.STDOUT, Stream.STDERR])
@pytest.mark.parametrize(
    ("wire_disposition", "expected"),
    [
        ("complete-capture", StreamDisposition.COMPLETE_CAPTURE),
        ("truncated-capture", StreamDisposition.TRUNCATED_CAPTURE),
    ],
)
def test_read_admits_exact_captured_prefix_under_persisted_bound(
    tmp_path: Path, stream: Stream, wire_disposition: str, expected: StreamDisposition
) -> None:
    database, repository, owner = _reserved(tmp_path, ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 3))
    row = repository.inspect(RUN)
    carrier = ScriptedCarrier(lambda request: _output_reply(request, disposition=wire_disposition, content=b"abc"))
    try:
        outcome = _read(repository, owner, carrier, stream=stream)
        assert outcome.accepted and outcome.disposition is expected
        assert outcome.output == b"abc"
        assert outcome.attempt.candidate is not None
        assert outcome.attempt.candidate.observation is not None
        assert outcome.attempt.candidate.observation.state is ManagedObservationState.AVAILABLE
        assert not outcome.attempt.requires_owner_retention
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
        assert b"abc" not in repr(outcome).encode()
    finally:
        database.close()


def test_read_rejects_capture_over_persisted_bound_but_preserves_raw_candidate(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path, ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 2))
    carrier = ScriptedCarrier(lambda request: _output_reply(request, disposition="truncated-capture", content=b"abc"))
    try:
        outcome = _read(repository, owner, carrier)
        assert not outcome.accepted and outcome.disposition is None and outcome.output is None
        assert outcome.attempt.candidate is not None
        assert outcome.attempt.candidate.observation is not None
        assert outcome.attempt.candidate.observation.state is ManagedObservationState.AVAILABLE
        assert outcome.attempt.candidate.observation.output == b"abc"
        assert carrier.calls == 1
    finally:
        database.close()


@pytest.mark.parametrize(
    ("mode", "wire_disposition", "expected"),
    [
        (ManagedOutputMode.DISCARD, "discarded", StreamDisposition.DISCARDED),
        (ManagedOutputMode.SENSITIVITY_SUPPRESSED, "sensitivity-suppressed", StreamDisposition.SUPPRESSED),
        (ManagedOutputMode.DISCARD, "sensitivity-suppressed", None),
        (ManagedOutputMode.SENSITIVITY_SUPPRESSED, "discarded", None),
    ],
)
def test_read_admits_only_matching_non_capture_policy(
    tmp_path: Path, mode: ManagedOutputMode, wire_disposition: str, expected: StreamDisposition | None
) -> None:
    database, repository, owner = _reserved(tmp_path, ManagedOutputPolicy(mode))
    carrier = ScriptedCarrier(lambda request: _output_reply(request, disposition=wire_disposition))
    try:
        outcome = _read(repository, owner, carrier)
        assert outcome.accepted is (expected is not None)
        assert outcome.disposition is expected and outcome.output is None
        assert outcome.attempt.candidate is not None
        assert outcome.attempt.candidate.observation is not None
        assert outcome.attempt.candidate.observation.state is ManagedObservationState.UNAVAILABLE
        assert carrier.calls == 1
    finally:
        database.close()


@pytest.mark.parametrize("fault", ["missing", "wrong-end", "wrong-bytes"])
def test_read_never_admits_missing_or_mismatched_evidence(tmp_path: Path, fault: str) -> None:
    database, repository, owner = _reserved(tmp_path)

    def response(request):  # type: ignore[no-untyped-def]
        if fault == "missing":
            return _records(request.nonce, ManagedResultControl((FactName.LAUNCH,)), (request.expected_launch,))
        if fault == "wrong-end":
            stale = _fact(FactName.STDOUT_END, _wire_launch(target="vm-other"), content=b"abc")
            return _records(
                request.nonce,
                ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
                (request.expected_launch, stale),
                b"abc",
            )
        return _output_reply(request, disposition="complete-capture", content=b"abc", delivered=b"abd")

    carrier = ScriptedCarrier(response)
    try:
        outcome = _read(repository, owner, carrier)
        assert not outcome.accepted and outcome.disposition is None and outcome.output is None
        assert outcome.attempt.candidate is not None
        assert outcome.attempt.candidate.observation is not None
        assert outcome.attempt.candidate.observation.state is not ManagedObservationState.AVAILABLE
        assert carrier.calls == 1
    finally:
        database.close()


@pytest.mark.parametrize("case", ["stream", "target", "absent"])
def test_read_refuses_before_borrow_and_carrier(tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(lambda request: b"")
    calls = 0
    original_borrow = owner.borrow

    def borrow_spy():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original_borrow()

    monkeypatch.setattr(owner, "borrow", borrow_spy)
    changes: dict[str, object] = {}
    if case == "stream":
        changes["stream"] = "stdout"
    elif case == "target":
        changes["target"] = replace(TARGET, incarnation="v1:" + "b" * 64)
    else:
        repository._connection.execute("DELETE FROM execution_runs WHERE run_id = ?", (RUN.run_id,))  # noqa: SLF001
    try:
        with pytest.raises(ValidationError):
            _read(repository, owner, carrier, **changes)
        assert calls == 0 and carrier.calls == 0
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
    finally:
        database.close()


def test_read_ambiguous_dispatch_retains_custody_and_does_not_admit_output(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    row = repository.inspect(RUN)
    carrier = ScriptedCarrier(
        lambda request: _output_reply(request, disposition="complete-capture", content=b"abc"),
        dispatch=Dispatch.UNKNOWN,
        code=None,
    )
    try:
        outcome = _read(repository, owner, carrier)
        assert not outcome.accepted and outcome.output is None
        assert outcome.attempt.pending_remote_effects and outcome.attempt.requires_owner_retention
        assert carrier.calls == 1 and repository.inspect(RUN) == row
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        database.close()
