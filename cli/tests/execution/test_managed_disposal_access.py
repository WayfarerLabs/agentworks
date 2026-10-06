"""Exact persisted-run admission and durable custody for private disposal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import ValidationError
from agentworks.execution import _managed_action_custody as access
from agentworks.execution._file_wire import FileRecordKind
from agentworks.execution._managed_disposal_access import (
    MANAGED_DISPOSAL_OBLIGATION_KIND,
    ManagedDisposalControlFact,
    decode_managed_disposal_obligation,
    dispose_bound_managed_run,
    encode_managed_disposal_obligation,
)
from agentworks.execution._managed_disposal_exchange import DisposalState
from agentworks.execution._managed_disposal_protocol import DisposalRequest, DisposalResult, encode_result
from agentworks.execution._managed_runs import (
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedRunReceipt,
)
from agentworks.execution._managed_start_operation import encode_managed_start_obligation
from agentworks.execution.carrier import Dispatch

from .test_managed_disposal import ExchangeCarrier, _disposed, _records
from .test_managed_observe_access import GUEST, RUN, TARGET, _options, _reserved

OBLIGATION_ID = "e" * 32


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


def _dispose(repository, owner, carrier: ExchangeCarrier, **changes: object):  # type: ignore[no-untyped-def]
    options = _options(owner, carrier, obligation_id=OBLIGATION_ID)
    options.update(changes)
    return dispose_bound_managed_run(repository, RUN, **options)  # type: ignore[arg-type]


def _not_ready(request: DisposalRequest) -> bytes:
    return _records(
        request.nonce,
        (
            (FileRecordKind.RESULT, encode_result(DisposalResult.NOT_READY)),
            (FileRecordKind.FINISHED, b""),
        ),
    )


def test_disposal_obligation_uses_exact_canonical_run_payload() -> None:
    expected = encode_managed_start_obligation(RUN.run_id)
    assert encode_managed_disposal_obligation(RUN.run_id) == expected
    assert decode_managed_disposal_obligation(expected) == RUN
    for invalid in (expected + b" ", expected.replace(b'"version":1', b'"version":2')):
        with pytest.raises(ValidationError):
            decode_managed_disposal_obligation(invalid)


@pytest.mark.parametrize("case", ["absent", "target", "guest", "unconfirmed", "wrong_owner"])
def test_preborrow_refusal_does_not_install_or_call_carrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ExchangeCarrier(_disposed)
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
            _dispose(repository, owner, carrier, **changes)
        assert borrow_calls == 0
        assert carrier.calls == 0
        assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("response", "state"),
    [(_not_ready, DisposalState.NOT_READY), (_disposed, DisposalState.DISPOSED)],
)
def test_validated_response_resolves_temporary_obligation_without_changing_run(
    tmp_path: Path,
    response: Callable[[DisposalRequest], bytes],
    state: DisposalState,
) -> None:
    database, repository, owner = _reserved(tmp_path)
    row = _confirmed(repository)
    carrier = ExchangeCarrier(response)
    try:
        outcome = _dispose(repository, owner, carrier)
        assert outcome.state is state
        assert not outcome.requires_owner_retention
        assert not outcome.pending_remote_effects
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.obligation_id == OBLIGATION_ID
        assert obligation.obligation_kind == MANAGED_DISPOSAL_OBLIGATION_KIND
        assert obligation.state is LifecycleObligationState.RESOLVED
        assert decode_managed_disposal_obligation(obligation.payload) == RUN
    finally:
        database.close()


def test_not_sent_resolves_without_disposal_claim(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = ExchangeCarrier(_disposed, dispatch=Dispatch.NOT_SENT)
    try:
        outcome = _dispose(repository, owner, carrier)
        assert outcome.state is None
        assert outcome.candidate is not None and outcome.candidate.dispatch is Dispatch.NOT_SENT
        assert not outcome.requires_owner_retention
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.RESOLVED
    finally:
        database.close()


@pytest.mark.parametrize("fault", ["unknown", "failed", "helper", "incomplete"])
def test_uncertain_or_failed_delivery_keeps_exact_disposal_obligation(tmp_path: Path, fault: str) -> None:
    database, repository, owner = _reserved(tmp_path)
    row = _confirmed(repository)

    def helper_failure(request: DisposalRequest) -> bytes:
        return _records(
            request.nonce,
            ((FileRecordKind.FAILED, b""), (FileRecordKind.FINISHED, b"")),
        )

    carrier = ExchangeCarrier(
        helper_failure if fault == "helper" else _disposed,
        dispatch=Dispatch.UNKNOWN if fault == "unknown" else Dispatch.SENT,
        code=1 if fault == "failed" else 0,
        complete=fault != "incomplete",
    )
    try:
        outcome = _dispose(repository, owner, carrier)
        assert outcome.state is None
        assert outcome.requires_owner_retention
        assert outcome.pending_remote_effects
        assert carrier.calls == 1
        assert repository.inspect(RUN) == row
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_managed_disposal_obligation(obligation.payload) == RUN
    finally:
        database.close()


def test_failed_pure_validation_resolves_unarmed_obligation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = ExchangeCarrier(_disposed)
    failure = KeyboardInterrupt("validation interrupted")

    def fail_validation(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(carrier, "validate", fail_validation)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _dispose(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedDisposalControlFact)
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
    carrier = ExchangeCarrier(_disposed)
    failure = KeyboardInterrupt("admission interrupted")
    owner_repository = owner._repository  # noqa: SLF001
    original_mark = owner_repository.mark_lifecycle_obligation_possible_effect

    def commit_then_interrupt(*args: object, **kwargs: object) -> object:
        original_mark(*args, **kwargs)  # type: ignore[arg-type]
        raise failure

    monkeypatch.setattr(owner_repository, "mark_lifecycle_obligation_possible_effect", commit_then_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _dispose(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedDisposalControlFact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.pending_remote_effects
        assert carrier.calls == 0
        assert carrier.validations >= 1
        (obligation,) = database.operations.list_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
    finally:
        database.close()


def test_carrier_exception_preserves_original_with_custody_fact(tmp_path: Path) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    failure = KeyboardInterrupt("carrier interrupted")

    class InterruptedCarrier(ExchangeCarrier):
        def execute(self, invocation, *, io, deadline):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise failure

    carrier = InterruptedCarrier(_disposed)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _dispose(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedDisposalControlFact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.pending_remote_effects
        assert carrier.calls == 1
    finally:
        database.close()


def test_registration_exception_preserves_original_and_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = ExchangeCarrier(_disposed)
    failure = KeyboardInterrupt("registration interrupted")
    probe = owner.borrow()
    borrow_type = type(probe)
    probe.close()

    def fail_registration(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(borrow_type, "install_dispatch_obligation", fail_registration)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _dispose(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedDisposalControlFact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.coordination_uncertain
        assert carrier.calls == 0
    finally:
        database.close()


def test_release_failure_preserves_original_with_custody_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    carrier = ExchangeCarrier(_disposed)
    failure = KeyboardInterrupt("release interrupted")

    def fail_release(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(access, "release_borrow_after_custody", fail_release)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            _dispose(repository, owner, carrier)
        assert raised.value is failure
        assert isinstance(failure.__cause__, ManagedDisposalControlFact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.coordination_uncertain
        assert carrier.calls == 1
    finally:
        database.close()
