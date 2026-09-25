"""Exact persisted-run admission and durable custody for private managed stop."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import ValidationError
from agentworks.execution import _managed_stop_access as access
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_runs import (
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedRunReceipt,
)
from agentworks.execution._managed_start_operation import (
    decode_managed_start_obligation,
    encode_managed_start_obligation,
)
from agentworks.execution._managed_stop_access import (
    MANAGED_STOP_OBLIGATION_KIND,
    ManagedStopControlFact,
    decode_managed_stop_obligation,
    encode_managed_stop_obligation,
    stop_bound_managed_run,
)
from agentworks.execution._managed_stop_exchange import ManagedStopState
from agentworks.execution._managed_stop_protocol import ManagedStopResult
from agentworks.execution.carrier import Dispatch

from .test_managed_observe_access import GUEST, RUN, TARGET, _options, _reserved
from .test_managed_stop import Carrier, _boundary, _records

OBLIGATION_ID = "e" * 32


def test_start_and_stop_obligations_share_exact_canonical_run_payload() -> None:
    expected = b'{"run_id":"' + RUN.run_id.encode("ascii") + b'","version":1}'
    assert encode_managed_start_obligation(RUN.run_id) == expected
    assert encode_managed_stop_obligation(RUN.run_id) == expected
    assert decode_managed_start_obligation(expected) == RUN
    assert decode_managed_stop_obligation(expected) == RUN
    for invalid in (expected + b" ", expected.replace(b'"version":1', b'"version":2')):
        with pytest.raises(ValidationError):
            decode_managed_stop_obligation(invalid)


def _confirmed(repository) -> object:  # type: ignore[no-untyped-def]
    reserved = repository.inspect(RUN)
    assert reserved is not None
    possible = repository.mark_possible_dispatch(reserved)
    confirmed = repository.reconcile(
        possible,
        ManagedLaunchObservation(
            Dispatch.SENT,
            ManagedRunReceipt(possible.identity, possible.identity.unit_name, possible.spec),
        ),
    )
    assert confirmed.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    return confirmed


def _stop(repository, owner, carrier: Carrier, **changes: object):  # type: ignore[no-untyped-def]
    options = _options(owner, carrier, obligation_id=OBLIGATION_ID)
    options.update(changes)
    return stop_bound_managed_run(repository, RUN, **options)  # type: ignore[arg-type]


def _response(request, *, terminated: bool = False) -> bytes:  # type: ignore[no-untyped-def]
    facts = (request.expected_launch, _boundary(request.expected_launch)) if terminated else (request.expected_launch,)
    names = (FactName.LAUNCH, FactName.BOUNDARY_EMPTY) if terminated else (FactName.LAUNCH,)
    return _records(request.nonce, ManagedStopResult(names), facts)


@pytest.mark.parametrize("case", ["absent", "target", "guest", "unconfirmed", "wrong_owner"])
def test_preborrow_refusal_does_not_install_or_call_carrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = Carrier(_response)
    changes: dict[str, object] = {}
    borrow_calls = 0
    original_borrow = owner.borrow

    def borrow_spy():  # type: ignore[no-untyped-def]
        nonlocal borrow_calls
        borrow_calls += 1
        return original_borrow()

    monkeypatch.setattr(owner, "borrow", borrow_spy)
    try:
        if case != "unconfirmed":
            _confirmed(repository)
        if case == "absent":
            repository._connection.execute("DELETE FROM execution_runs WHERE run_id = ?", (RUN.run_id,))  # noqa: SLF001
        elif case == "target":
            changes["target"] = replace(TARGET, incarnation="v1:" + "b" * 64)
        elif case == "guest":
            changes["guest"] = replace(GUEST, init_start_ticks=1235)
        elif case == "wrong_owner":
            repository._connection.execute(  # noqa: SLF001
                "UPDATE execution_runs SET owner_kind = ?, owner_id = ?, lifetime = ? WHERE run_id = ?",
                ("operation", RUN.run_id, "operation", RUN.run_id),
            )
        with pytest.raises(ValidationError):
            _stop(repository, owner, carrier, **changes)
        assert borrow_calls == 0
        assert carrier.calls == 0
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
    finally:
        database.close()


@pytest.mark.parametrize("terminated", [False, True])
def test_validated_response_resolves_temporary_obligation_but_not_run(tmp_path: Path, terminated: bool) -> None:
    database, repository, owner = _reserved(tmp_path)
    row = _confirmed(repository)
    carrier = Carrier(lambda request: _response(request, terminated=terminated))
    try:
        outcome = _stop(repository, owner, carrier)
        assert outcome.state is (ManagedStopState.TERMINATED if terminated else ManagedStopState.ACCEPTED)
        assert not outcome.requires_owner_retention
        assert not outcome.pending_remote_effects
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.obligation_id == OBLIGATION_ID
        assert obligation.obligation_kind == MANAGED_STOP_OBLIGATION_KIND
        assert obligation.state is LifecycleObligationState.RESOLVED
        assert obligation.payload_version == 1
        assert decode_managed_stop_obligation(obligation.payload) == RUN
    finally:
        database.close()


def test_not_sent_settles_and_resolves_without_stop_evidence(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = Carrier(_response, dispatch=Dispatch.NOT_SENT)
    try:
        outcome = _stop(repository, owner, carrier)
        assert outcome.state is None
        assert outcome.candidate is not None and outcome.candidate.dispatch is Dispatch.NOT_SENT
        assert not outcome.requires_owner_retention
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.RESOLVED
    finally:
        database.close()


@pytest.mark.parametrize("fault", ["unknown", "helper", "invalid"])
def test_uncertain_or_failed_delivery_keeps_exact_stop_obligation(tmp_path: Path, fault: str) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = Carrier(
        (lambda request: _records(request.nonce, ManagedStopResult((FactName.LAUNCH,)), (b"stale",)))
        if fault == "invalid"
        else (lambda request: b"AGWF1" if fault == "helper" else _response(request)),
        dispatch=Dispatch.UNKNOWN if fault == "unknown" else Dispatch.SENT,
    )
    try:
        outcome = _stop(repository, owner, carrier)
        assert outcome.state is None
        assert outcome.requires_owner_retention
        assert outcome.pending_remote_effects
        assert carrier.calls == 1
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_managed_stop_obligation(obligation.payload) == RUN
    finally:
        database.close()


def test_failed_pure_validation_resolves_unarmed_obligation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = Carrier(_response)
    failure = KeyboardInterrupt("validation interrupted")

    def fail_validation(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(carrier, "validate", fail_validation)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _stop(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedStopControlFact)
        assert not failure.__cause__.outcome.requires_owner_retention
        assert carrier.calls == 0
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.RESOLVED
    finally:
        database.close()


def test_commit_then_interrupted_begin_attempt_keeps_original_control_and_possible_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = Carrier(_response)
    failure = KeyboardInterrupt("admission interrupted")
    owner_repository = owner._repository  # noqa: SLF001
    original_mark = owner_repository.mark_lifecycle_obligation_possible_effect

    def commit_then_interrupt(*args: object, **kwargs: object) -> object:
        original_mark(*args, **kwargs)  # type: ignore[arg-type]
        raise failure

    monkeypatch.setattr(owner_repository, "mark_lifecycle_obligation_possible_effect", commit_then_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _stop(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedStopControlFact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.pending_remote_effects
        assert carrier.calls == 0
        assert carrier.validations >= 1
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
    finally:
        database.close()


def test_release_failure_preserves_original_with_custody_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = Carrier(_response)
    failure = KeyboardInterrupt("release interrupted")

    def fail_release(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(access, "release_borrow_after_custody", fail_release)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _stop(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedStopControlFact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.coordination_uncertain
        assert carrier.calls == 1
    finally:
        database.close()
