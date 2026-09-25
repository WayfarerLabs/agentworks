"""Exact persisted-run admission and custody for private managed observation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_protocol import decode_managed_job_fact
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_observation_exchange import ManagedObservationState
from agentworks.execution._managed_observation_protocol import ManagedResultControl
from agentworks.execution._managed_observe_access import ManagedObserveControlFact, observe_bound_managed_run
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

from .test_managed_observation import ScriptedCarrier, _records

RUN = ManagedRunIdentity("a" * 32)
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)
TARGET = ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", "v1:" + "c" * 64, vm_guest_boot_id(GUEST))
ROOT_PLAN = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)


def _reserved(tmp_path: Path) -> tuple[Database, ManagedRunRepository, OperationOwner]:
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
        output_policy=ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096),
        identity=RUN,
    )
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "observe")
    return database, repository, owner


def _observe(repository: ManagedRunRepository, owner: OperationOwner, carrier: ScriptedCarrier, **changes: object):  # type: ignore[no-untyped-def]
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
    return observe_bound_managed_run(repository, RUN, **options)  # type: ignore[arg-type]


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


def test_missing_facts_remain_unknown_without_replay(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(
        lambda request: _records(request.nonce, ManagedResultControl((FactName.LAUNCH,)), (request.expected_launch,))
    )
    try:
        outcome = _observe(repository, owner, carrier)
        assert outcome.candidate is not None
        assert outcome.candidate.observation is not None
        assert outcome.candidate.observation.state is ManagedObservationState.OBSERVED
        assert tuple(name for name, _ in outcome.candidate.observation.facts) == (FactName.LAUNCH,)
        assert carrier.calls == 1
        assert repository.inspect(RUN) is not None
    finally:
        database.close()


@pytest.mark.parametrize("case", ["absent", "target", "boot", "operation", "platform"])
def test_refuses_wrong_row_or_target_before_borrow_and_carrier(tmp_path: Path, case: str) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(lambda request: b"")
    changes: dict[str, object] = {}
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


def test_carrier_base_exception_preserves_original_with_custody_fact(tmp_path: Path) -> None:
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
            _observe(repository, owner, carrier)
        assert raised.value is interrupted
        assert isinstance(raised.value.__cause__, ManagedObserveControlFact)
        assert raised.value.__cause__.outcome.requires_owner_retention
        assert raised.value.__cause__.outcome.pending_remote_effects
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
    finally:
        database.close()
