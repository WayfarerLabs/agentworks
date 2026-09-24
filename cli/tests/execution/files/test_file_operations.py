"""Owned composition checks for private single-file exchanges."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationClaimState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _file_operations as operations
from agentworks.execution._account import (
    AccountObservationState,
    FileOwnershipObservation,
    FileOwnershipResolutionResult,
)
from agentworks.execution._account_protocol import FileOwnership, FileOwnershipFailure
from agentworks.execution._file_inventory_exchange import (
    FileInventoryCandidateResult,
    FileInventoryObservation,
    FileInventoryObservationState,
)
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataObservation,
    FileMetadataObservationState,
)
from agentworks.execution._file_metadata_protocol import FileMetadataOperation
from agentworks.execution._file_object_exchange import (
    FileObjectCandidateResult,
    FileObjectObservation,
    FileObjectObservationState,
)
from agentworks.execution._file_objects import FileKind
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.operations import OperationAttempt, OperationBorrow, OperationOwner

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed file helpers require Linux")

_READY = RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3")
_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_NORMAL_EXIT = ExitStatus(code=0)
_REVISION = FileRevision(FileStat(1, 2, 0o100600, 1, 1001, 1002, 0, 3, 4))


class ControlStop(BaseException):
    pass


class SyntheticCarrier:
    def __init__(
        self,
        dispatch: Dispatch = Dispatch.SENT,
        completion: ExitStatus | None = _NORMAL_EXIT,
        *,
        control: BaseException | None = None,
        expire_on_return: bool = False,
    ) -> None:
        self.dispatch = dispatch
        self.completion = completion
        self.control = control
        self.expire_on_return = expire_on_return
        self.calls = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        del invocation, io
        self.calls += 1
        if self.control is not None:
            raise self.control
        if self.expire_on_return:
            object.__setattr__(deadline, "expires_at", 0.0)
        return CarrierReport(
            self.dispatch,
            self.completion,
            local_status=0,
            stdout=CapturedOutput(),
            stderr=CapturedOutput(),
        )


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[tuple[Database, OperationOwner, OperationBorrow]]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-operation-vm"),
        "file-operation",
    )
    try:
        yield database, owner, owner.borrow()
    finally:
        database.close()


def _report(operation: BorrowedFixedHelperCarrier, deadline: Deadline) -> CarrierReport:
    return operation.execute(PreparedInvocation(("fixed-helper",)), io=CarrierIO(), deadline=deadline)


def _object_result(
    operation: BorrowedFixedHelperCarrier,
    deadline: Deadline,
    observation: FileObjectObservation,
) -> FileObjectCandidateResult:
    report = _report(operation, deadline)
    return FileObjectCandidateResult(
        report.dispatch,
        report.completion,
        report.local_status,
        report.failure,
        _READY,
        observation,
    )


@pytest.mark.parametrize(
    ("prepare", "exchange_name", "result"),
    [
        (
            operations._prepare_stat,
            "exchange_stat_file",
            FileObjectCandidateResult(
                Dispatch.SENT,
                ExitStatus(code=0),
                0,
                None,
                _READY,
                FileObjectObservation(FileObjectObservationState.ABSENT),
            ),
        ),
        (
            operations._prepare_inventory,
            "exchange_list_directory",
            FileInventoryCandidateResult(
                Dispatch.SENT,
                ExitStatus(code=0),
                0,
                None,
                _READY,
                FileInventoryObservation(FileInventoryObservationState.NOT_FOUND),
            ),
        ),
        (
            operations._prepare_remove,
            "exchange_remove_file",
            FileObjectCandidateResult(
                Dispatch.SENT,
                ExitStatus(code=0),
                0,
                None,
                _READY,
                FileObjectObservation(FileObjectObservationState.UNCHANGED),
            ),
        ),
    ],
    ids=["stat", "list", "remove"],
)
def test_single_exchange_preparations_preserve_typed_result_and_settle(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    prepare,
    exchange_name: str,
    result,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier()
    seen_operations: list[BorrowedFixedHelperCarrier] = []

    def exchange(operation: BorrowedFixedHelperCarrier, **kwargs: object):
        deadline = kwargs["deadline"]
        assert isinstance(deadline, Deadline)
        report = _report(operation, deadline)
        seen_operations.append(operation)
        return replace(
            result,
            dispatch=report.dispatch,
            carrier_completion=report.completion,
            carrier_local_status=report.local_status,
        )

    monkeypatch.setattr(operations, exchange_name, exchange)
    arguments = {
        "trusted_root_path": "/approved",
        "relative_path": "target",
        "plan": _PLAN,
        "deadline": Deadline.after(15),
        "runtime_selection": _RUNTIME,
        "borrow": borrow,
    }
    if prepare is operations._prepare_inventory:
        arguments.update(max_entries=10, max_depth=1, max_encoded_bytes=4096)
    elif prepare is operations._prepare_remove:
        arguments.update(expected_kind=FileKind.REGULAR, expected_revision=_REVISION)

    outcome = prepare(carrier, **arguments).run()

    assert outcome.result == result
    assert outcome.ownership_result is None
    assert not outcome.deadline_exceeded
    assert not outcome.requires_owner_retention
    assert carrier.calls == 1 and len(seen_operations) == 1
    with pytest.raises(StateError):
        owner.close()
    borrow.close()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("dispatch", "completion", "retained"),
    [
        (Dispatch.NOT_SENT, None, False),
        (Dispatch.SENT, ExitStatus(code=0), False),
        (Dispatch.SENT, ExitStatus(code=7), True),
        (Dispatch.SENT, None, True),
        (Dispatch.UNKNOWN, None, True),
    ],
)
def test_settlement_uses_only_supported_termination_evidence(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    dispatch: Dispatch,
    completion: ExitStatus | None,
    retained: bool,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier(dispatch, completion)

    def exchange(operation: BorrowedFixedHelperCarrier, **kwargs: object) -> FileObjectCandidateResult:
        deadline = kwargs["deadline"]
        assert isinstance(deadline, Deadline)
        return _object_result(operation, deadline, FileObjectObservation(FileObjectObservationState.ABSENT))

    monkeypatch.setattr(operations, "exchange_stat_file", exchange)

    outcome = operations._prepare_stat(
        carrier,
        trusted_root_path="/approved",
        relative_path="target",
        plan=_PLAN,
        deadline=Deadline.after(15),
        runtime_selection=_RUNTIME,
        borrow=borrow,
    ).run()

    assert outcome.result is not None and outcome.result.dispatch is dispatch
    assert outcome.pending_remote_effects is retained
    assert outcome.requires_owner_retention is retained
    if not retained:
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


def test_local_preparation_failure_never_arms_or_dispatches(
    owned: tuple[Database, OperationOwner, OperationBorrow],
) -> None:
    database, owner, borrow = owned
    carrier = SyntheticCarrier()

    with pytest.raises(ValidationError) as raised:
        operations._prepare_inventory(
            carrier,
            trusted_root_path="/approved",
            relative_path="target",
            max_entries=0,
            max_depth=1,
            max_encoded_bytes=1024,
            plan=_PLAN,
            deadline=Deadline.after(15),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        ).run()

    fact = raised.value.__cause__
    assert isinstance(fact, operations.OwnedFileControlFact)
    assert fact.outcome.result is None
    assert not fact.outcome.requires_owner_retention
    assert carrier.calls == 0
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    with pytest.raises(StateError):
        owner.borrow()
    borrow.close()
    owner.close()


def test_expired_deadline_returns_without_dispatch_and_preserves_borrow(
    owned: tuple[Database, OperationOwner, OperationBorrow],
) -> None:
    database, owner, borrow = owned
    carrier = SyntheticCarrier()

    outcome = operations._prepare_stat(
        carrier,
        trusted_root_path="/approved",
        relative_path="target",
        plan=_PLAN,
        deadline=Deadline.after(0),
        runtime_selection=_RUNTIME,
        borrow=borrow,
    ).run()

    assert outcome.result is None and outcome.deadline_exceeded
    assert not outcome.requires_owner_retention and carrier.calls == 0
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    with pytest.raises(StateError):
        owner.close()
    borrow.close()
    owner.close()


@pytest.mark.parametrize("completion", [ExitStatus(code=0), ExitStatus(code=9)], ids=["normal", "abnormal"])
def test_deadline_after_return_is_independent_of_operation_facts(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    completion: ExitStatus,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier(completion=completion, expire_on_return=True)

    def exchange(operation: BorrowedFixedHelperCarrier, **kwargs: object) -> FileObjectCandidateResult:
        deadline = kwargs["deadline"]
        assert isinstance(deadline, Deadline)
        return _object_result(operation, deadline, FileObjectObservation(FileObjectObservationState.ABSENT))

    monkeypatch.setattr(operations, "exchange_stat_file", exchange)
    outcome = operations._prepare_stat(
        carrier,
        trusted_root_path="/approved",
        relative_path="target",
        plan=_PLAN,
        deadline=Deadline.after(15),
        runtime_selection=_RUNTIME,
        borrow=borrow,
    ).run()

    assert outcome.result is not None and outcome.deadline_exceeded
    assert outcome.requires_owner_retention is (completion.code != 0)
    if not outcome.requires_owner_retention:
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


@pytest.mark.parametrize("entrypoint_name", ["set_metadata", "ensure_directory"])
def test_metadata_lookup_and_mutation_share_one_borrow_and_preserve_both_results(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint_name: str,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier()
    operations_seen: list[BorrowedFixedHelperCarrier] = []

    def resolve(
        operation: BorrowedFixedHelperCarrier,
        trusted_owner: str,
        trusted_group: str,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileOwnershipResolutionResult:
        del trusted_owner, trusted_group, runtime_selection
        report = _report(operation, deadline)
        operations_seen.append(operation)
        return FileOwnershipResolutionResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            _READY,
            FileOwnershipObservation(
                AccountObservationState.RESOLVED,
                ownership=FileOwnership(123, 456),
            ),
        )

    operation_result = FileMetadataCandidateResult(
        Dispatch.SENT,
        ExitStatus(code=0),
        0,
        None,
        _READY,
        FileMetadataObservation(FileMetadataObservationState.UNCHANGED),
    )

    def mutate(operation: BorrowedFixedHelperCarrier, **kwargs: object) -> FileMetadataCandidateResult:
        assert kwargs["uid"] == 123 and kwargs["gid"] == 456
        deadline = kwargs["deadline"]
        assert isinstance(deadline, Deadline)
        report = _report(operation, deadline)
        operations_seen.append(operation)
        return replace(
            operation_result,
            dispatch=report.dispatch,
            carrier_completion=report.completion,
            carrier_local_status=report.local_status,
        )

    monkeypatch.setattr(operations, "resolve_file_ownership", resolve)
    monkeypatch.setattr(
        operations,
        "set_file_metadata" if entrypoint_name == "set_metadata" else "ensure_file_directory",
        mutate,
    )
    metadata_operation = (
        FileMetadataOperation.SET_METADATA
        if entrypoint_name == "set_metadata"
        else FileMetadataOperation.ENSURE_DIRECTORY
    )

    outcome = operations._prepare_metadata(
        carrier,
        operation=metadata_operation,
        trusted_root_path="/approved",
        relative_path="target",
        trusted_owner="owner",
        trusted_group="group",
        mode=0o750,
        plan=_PLAN,
        deadline=Deadline.after(15),
        runtime_selection=_RUNTIME,
        borrow=borrow,
    ).run()

    assert outcome.ownership_result is not None
    assert outcome.ownership_result.observation is not None
    assert outcome.result == operation_result
    assert len(operations_seen) == 2 and operations_seen[0] is operations_seen[1]
    assert carrier.calls == 2 and not outcome.requires_owner_retention
    borrow.close()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


@pytest.mark.parametrize(
    ("runtime", "observation", "completion", "deadline_expired"),
    [
        (
            _READY,
            FileOwnershipObservation(
                AccountObservationState.REFUSED,
                failure=FileOwnershipFailure.MISSING_OWNER,
            ),
            ExitStatus(code=0),
            False,
        ),
        (
            RuntimePrerequisiteObservation(RuntimePrerequisiteState.MISSING, None),
            None,
            ExitStatus(code=0),
            False,
        ),
        (_READY, FileOwnershipObservation(AccountObservationState.INCOMPLETE), ExitStatus(code=0), False),
        (
            _READY,
            FileOwnershipObservation(
                AccountObservationState.RESOLVED,
                ownership=FileOwnership(123, 456),
            ),
            ExitStatus(code=7),
            False,
        ),
        (
            _READY,
            FileOwnershipObservation(
                AccountObservationState.RESOLVED,
                ownership=FileOwnership(123, 456),
            ),
            ExitStatus(code=0),
            True,
        ),
    ],
    ids=["refused", "runtime", "incomplete", "abnormal", "expired"],
)
def test_metadata_lookup_must_be_ready_resolved_normal_and_within_deadline(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    runtime: RuntimePrerequisiteObservation,
    observation: FileOwnershipObservation | None,
    completion: ExitStatus,
    deadline_expired: bool,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier(completion=completion, expire_on_return=deadline_expired)
    mutation_calls = 0

    def resolve(
        operation: BorrowedFixedHelperCarrier,
        trusted_owner: str,
        trusted_group: str,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileOwnershipResolutionResult:
        del trusted_owner, trusted_group, runtime_selection
        report = _report(operation, deadline)
        return FileOwnershipResolutionResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            runtime,
            observation,
        )

    def mutate(operation: BorrowedFixedHelperCarrier, **kwargs: object) -> FileMetadataCandidateResult:
        nonlocal mutation_calls
        del operation, kwargs
        mutation_calls += 1
        raise AssertionError("metadata mutation must remain gated")

    monkeypatch.setattr(operations, "resolve_file_ownership", resolve)
    monkeypatch.setattr(operations, "set_file_metadata", mutate)

    outcome = operations._prepare_metadata(
        carrier,
        operation=FileMetadataOperation.SET_METADATA,
        trusted_root_path="/approved",
        relative_path="target",
        trusted_owner="owner",
        trusted_group="group",
        mode=0o640,
        plan=_PLAN,
        deadline=Deadline.after(15),
        runtime_selection=_RUNTIME,
        borrow=borrow,
    ).run()

    assert outcome.ownership_result is not None and outcome.result is None
    assert outcome.deadline_exceeded is deadline_expired
    assert mutation_calls == 0 and carrier.calls == 1
    retained = completion != ExitStatus(code=0)
    assert outcome.requires_owner_retention is retained
    if not retained:
        borrow.close()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


@pytest.mark.parametrize(
    ("trusted_owner", "mode"),
    [("bad\0owner", 0o640), ("owner", -1)],
    ids=["owner", "mode"],
)
def test_metadata_canonical_validation_precedes_lookup(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    trusted_owner: str,
    mode: int,
) -> None:
    database, owner, borrow = owned
    carrier = SyntheticCarrier()
    lookup_calls = 0

    def resolve(*args: object, **kwargs: object) -> FileOwnershipResolutionResult:
        nonlocal lookup_calls
        del args, kwargs
        lookup_calls += 1
        raise AssertionError("invalid metadata must not start lookup")

    monkeypatch.setattr(operations, "resolve_file_ownership", resolve)

    with pytest.raises(ValidationError):
        operations._prepare_metadata(
            carrier,
            operation=FileMetadataOperation.SET_METADATA,
            trusted_root_path="/approved",
            relative_path="target",
            trusted_owner=trusted_owner,
            trusted_group="group",
            mode=mode,
            plan=_PLAN,
            deadline=Deadline.after(15),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        ).run()

    assert lookup_calls == 0 and carrier.calls == 0
    claim = database.operations.inspect(owner.ownership.scope)
    assert claim is not None and claim.state is OperationClaimState.RESERVED
    borrow.close()
    owner.close()


def test_settlement_failure_retains_previously_returned_fact(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier()
    observation = FileObjectObservation(FileObjectObservationState.ABSENT)

    def exchange(operation: BorrowedFixedHelperCarrier, **kwargs: object) -> FileObjectCandidateResult:
        deadline = kwargs["deadline"]
        assert isinstance(deadline, Deadline)
        return _object_result(operation, deadline, observation)

    def fail_settle(attempt: OperationAttempt) -> None:
        del attempt
        raise RuntimeError("settlement-canary")

    monkeypatch.setattr(operations, "exchange_stat_file", exchange)
    monkeypatch.setattr(OperationAttempt, "settle", fail_settle)

    with pytest.raises(RuntimeError, match="settlement-canary") as raised:
        operations._prepare_stat(
            carrier,
            trusted_root_path="/approved",
            relative_path="target",
            plan=_PLAN,
            deadline=Deadline.after(15),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        ).run()

    fact = raised.value.__cause__
    assert isinstance(fact, operations.OwnedFileControlFact)
    assert fact.outcome.result is not None and fact.outcome.result.observation is observation
    assert fact.outcome.coordination_uncertain
    assert fact.outcome.requires_owner_retention


def test_metadata_lookup_fact_is_recorded_before_settlement(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier()
    ownership_result: FileOwnershipResolutionResult | None = None

    def resolve(
        operation: BorrowedFixedHelperCarrier,
        trusted_owner: str,
        trusted_group: str,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileOwnershipResolutionResult:
        nonlocal ownership_result
        del trusted_owner, trusted_group, runtime_selection
        report = _report(operation, deadline)
        ownership_result = FileOwnershipResolutionResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            _READY,
            FileOwnershipObservation(
                AccountObservationState.RESOLVED,
                ownership=FileOwnership(123, 456),
            ),
        )
        return ownership_result

    def fail_settle(attempt: OperationAttempt) -> None:
        del attempt
        raise RuntimeError("lookup-settlement-canary")

    monkeypatch.setattr(operations, "resolve_file_ownership", resolve)
    monkeypatch.setattr(OperationAttempt, "settle", fail_settle)

    with pytest.raises(RuntimeError, match="lookup-settlement-canary") as raised:
        operations._prepare_metadata(
            carrier,
            operation=FileMetadataOperation.SET_METADATA,
            trusted_root_path="/approved",
            relative_path="target",
            trusted_owner="owner",
            trusted_group="group",
            mode=0o640,
            plan=_PLAN,
            deadline=Deadline.after(15),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        ).run()

    fact = raised.value.__cause__
    assert isinstance(fact, operations.OwnedFileControlFact)
    assert fact.outcome.ownership_result is ownership_result
    assert fact.outcome.result is None
    assert fact.outcome.coordination_uncertain
    assert fact.outcome.requires_owner_retention


@pytest.mark.parametrize("control", [RuntimeError("ordinary-canary"), ControlStop("base-canary")])
def test_escaping_carrier_control_retains_safe_private_facts(
    owned: tuple[Database, OperationOwner, OperationBorrow],
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
) -> None:
    _, owner, borrow = owned
    carrier = SyntheticCarrier(control=control)

    def exchange(operation: BorrowedFixedHelperCarrier, **kwargs: object) -> FileObjectCandidateResult:
        deadline = kwargs["deadline"]
        assert isinstance(deadline, Deadline)
        return _object_result(operation, deadline, FileObjectObservation(FileObjectObservationState.ABSENT))

    monkeypatch.setattr(operations, "exchange_stat_file", exchange)

    with pytest.raises(type(control)) as raised:
        operations._prepare_stat(
            carrier,
            trusted_root_path="/approved/private-canary",
            relative_path="target-canary",
            plan=_PLAN,
            deadline=Deadline.after(15),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        ).run()

    assert raised.value is control
    fact = raised.value.__cause__
    assert isinstance(fact, operations.OwnedFileControlFact)
    assert fact.outcome.result is None
    assert fact.outcome.pending_remote_effects
    assert fact.outcome.requires_owner_retention
    assert "canary" not in repr(fact) and "canary" not in repr(fact.outcome)
    with pytest.raises(StateError):
        owner.close()
    borrow.handoff_unresolved()
    with pytest.raises(StateError):
        owner.close()


def test_owner_close_during_dispatch_retains_the_interrupted_attempt(
    owned: tuple[Database, OperationOwner, OperationBorrow],
) -> None:
    _, owner, borrow = owned

    class ClosingCarrier(SyntheticCarrier):
        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.validate(invocation, io=io)
            del invocation, io, deadline
            self.calls += 1
            owner.close()
            raise AssertionError("active owner close must refuse")

    carrier = ClosingCarrier()

    with pytest.raises(StateError) as raised:
        operations._prepare_stat(
            carrier,
            trusted_root_path="/approved",
            relative_path="target",
            plan=_PLAN,
            deadline=Deadline.after(15),
            runtime_selection=_RUNTIME,
            borrow=borrow,
        ).run()

    fact = raised.value.__cause__
    assert isinstance(fact, operations.OwnedFileControlFact)
    assert fact.outcome.pending_remote_effects
    assert fact.outcome.requires_owner_retention
    assert carrier.calls == 1
