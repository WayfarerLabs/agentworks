"""Interrupted obligation and release custody for managed disposal."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from agentworks.db import LifecycleObligationState, OperationOwnership
from agentworks.execution import _managed_action_custody as custody
from agentworks.execution._managed_disposal_access import (
    ManagedDisposalControlFact,
    ManagedDisposalOutcome,
    dispose_bound_managed_run,
)
from agentworks.execution._managed_runs import ManagedRunRepository
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import Deadline
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner, _PreRegistrationRefusal

from .test_managed_disposal import ExchangeCarrier, _disposed
from .test_managed_disposal_access import _confirmed
from .test_managed_observe_access import GUEST, RESOURCE_OWNER, ROOT_PLAN, RUN, TARGET, _reserved


def _action(
    repository: ManagedRunRepository, owner: OperationOwner
) -> tuple[
    Callable[[], ManagedDisposalOutcome],
    ExchangeCarrier,
    type[ManagedDisposalControlFact],
]:
    deadline = Deadline.after(10)
    runtime_selection = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
    disposal_carrier = ExchangeCarrier(_disposed)

    def invoke_disposal() -> ManagedDisposalOutcome:
        return dispose_bound_managed_run(
            repository,
            RUN,
            target=TARGET,
            guest=GUEST,
            root_plan=ROOT_PLAN,
            carrier=disposal_carrier,
            runtime_selection=runtime_selection,
            deadline=deadline,
            owner=owner,
            expected_resource_owner=RESOURCE_OWNER,
            obligation_id="f" * 32,
        )

    return invoke_disposal, disposal_carrier, ManagedDisposalControlFact


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_registration_preserves_original_and_uncertain_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    invoke, carrier, control_fact = _action(repository, owner)
    failure = KeyboardInterrupt("registration interrupted")
    owner_repository = owner._repository  # noqa: SLF001
    original_register = owner_repository.register_lifecycle_obligation

    def interrupt_registration(
        ownership: OperationOwnership,
        obligation_kind: str,
        payload_version: int,
        payload: bytes,
        *,
        obligation_id: str | None = None,
    ) -> NoReturn:
        if after_commit:
            original_register(
                ownership,
                obligation_kind,
                payload_version,
                payload,
                obligation_id=obligation_id,
            )
        raise failure

    monkeypatch.setattr(owner_repository, "register_lifecycle_obligation", interrupt_registration)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            invoke()
        assert raised.value is failure
        assert isinstance(failure.__cause__, control_fact)
        outcome = failure.__cause__.outcome
        assert outcome.requires_owner_retention
        assert outcome.coordination_uncertain
        assert carrier.calls == 0
        obligations = database.operations.list_pending_lifecycle_obligations(owner.ownership)
        assert len(obligations) == int(after_commit)
        if after_commit:
            assert obligations[0].state is LifecycleObligationState.REGISTERED
    finally:
        database.close()


def test_interrupted_release_after_reply_is_attempted_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    invoke, carrier, control_fact = _action(repository, owner)
    failure = KeyboardInterrupt("release interrupted")
    release_calls = 0

    def interrupt_release(*_args: object, **_kwargs: object) -> None:
        nonlocal release_calls
        release_calls += 1
        raise failure

    monkeypatch.setattr(custody, "release_borrow_after_custody", interrupt_release)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            invoke()
        assert raised.value is failure
        assert isinstance(failure.__cause__, control_fact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.coordination_uncertain
        assert release_calls == 1
        assert carrier.calls == 1
    finally:
        database.close()


def test_interrupted_carrier_before_reply_retains_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    invoke, carrier, control_fact = _action(repository, owner)
    failure = KeyboardInterrupt("carrier interrupted")

    def interrupt_carrier(*_args: object, **_kwargs: object) -> None:
        carrier.calls += 1
        raise failure

    monkeypatch.setattr(carrier, "execute", interrupt_carrier)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            invoke()
        assert raised.value is failure
        assert isinstance(failure.__cause__, control_fact)
        assert failure.__cause__.outcome.requires_owner_retention
        assert failure.__cause__.outcome.pending_remote_effects
        assert failure.__cause__.outcome.coordination_uncertain
        assert carrier.calls == 1
        (obligation,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
        assert obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
    finally:
        database.close()


def test_pre_registration_closing_refusal_closes_borrow_without_control_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _reserved(tmp_path)
    _confirmed(repository)
    invoke, carrier, _ = _action(repository, owner)
    original_install = OperationBorrow.install_dispatch_obligation

    def close_then_register(
        self: OperationBorrow,
        obligation_id: str,
        obligation_kind: str,
        *,
        payload_version: int,
        payload: bytes,
    ) -> LifecycleObligation:
        self._owner.stop_admission()  # noqa: SLF001
        return original_install(
            self,
            obligation_id,
            obligation_kind,
            payload_version=payload_version,
            payload=payload,
        )

    monkeypatch.setattr(OperationBorrow, "install_dispatch_obligation", close_then_register)
    try:
        with pytest.raises(_PreRegistrationRefusal) as raised:
            invoke()
        assert raised.value.__cause__ is None
        assert carrier.calls == 0
        assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
    finally:
        database.close()
