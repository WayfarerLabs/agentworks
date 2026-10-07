"""Core-supplied resource namespaces fence private independent job controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agentworks.db import OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._managed_disposal_access import dispose_bound_managed_run
from agentworks.execution._managed_disposal_exchange import DisposalState
from agentworks.execution._managed_job_store import Stream
from agentworks.execution._managed_observe_access import (
    observe_and_reconcile_bound_managed_run,
    observe_bound_managed_run,
    read_bound_managed_output,
)
from agentworks.execution._managed_result import collect_bound_managed_result, wait_bound_managed_result
from agentworks.execution._managed_runs import ManagedRunLifetime, ManagedRunOwner, ManagedRunOwnerKind
from agentworks.execution._managed_stop_access import stop_bound_managed_run
from agentworks.execution._managed_stop_exchange import ManagedStopState
from agentworks.operations import OperationOwner

from .test_managed_disposal import ExchangeCarrier, _disposed
from .test_managed_observation import ScriptedCarrier
from .test_managed_observe_access import RESOURCE_OWNER, RUN, _options, _reserved
from .test_managed_result import _confirmed, _reply
from .test_managed_stop import Carrier as StopCarrier
from .test_managed_stop_access import _response

CONTROLS: tuple[Callable[..., object], ...] = (
    observe_bound_managed_run,
    observe_and_reconcile_bound_managed_run,
    read_bound_managed_output,
    collect_bound_managed_result,
    wait_bound_managed_result,
    stop_bound_managed_run,
    dispose_bound_managed_run,
)


def _arguments(control: Callable[..., object]) -> dict[str, object]:
    if control is read_bound_managed_output:
        return {"stream": Stream.STDOUT}
    if control in (stop_bound_managed_run, dispose_bound_managed_run):
        return {"obligation_id": "e" * 32}
    return {}


@pytest.mark.parametrize("control", CONTROLS)
@pytest.mark.parametrize(
    "binding",
    (None, {}, "session-7", ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32)),
)
def test_invalid_binding_refuses_before_repository_lookup(
    tmp_path: Path, control: Callable[..., object], binding: object
) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(_reply)
    try:
        with (
            patch.object(repository, "inspect", wraps=repository.inspect) as inspect,
            patch.object(owner, "borrow", wraps=owner.borrow) as borrow,
            pytest.raises(ValidationError),
        ):
            control(repository, RUN, **_options(owner, carrier, expected_resource_owner=binding), **_arguments(control))
        inspect.assert_not_called()
        borrow.assert_not_called()
        assert carrier.calls == 0
        assert owner.list_pending_lifecycle_obligations() == ()
    finally:
        database.close()


@pytest.mark.parametrize("control", CONTROLS)
def test_same_vm_foreign_resource_refuses_without_borrow_or_delivery(
    tmp_path: Path, control: Callable[..., object]
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(_reply)
    try:
        before = repository.inspect(RUN)
        foreign = ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-8")
        with patch.object(owner, "borrow", wraps=owner.borrow) as borrow, pytest.raises(ValidationError):
            control(repository, RUN, **_options(owner, carrier, expected_resource_owner=foreign), **_arguments(control))
        borrow.assert_not_called()
        assert carrier.calls == 0
        assert repository.inspect(RUN) == before
        assert owner.list_pending_lifecycle_obligations() == ()
    finally:
        database.close()


@pytest.mark.parametrize("control", CONTROLS)
def test_resource_binding_cannot_adopt_operation_run(tmp_path: Path, control: Callable[..., object]) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(_reply)
    try:
        record = repository.inspect(RUN)
        assert record is not None
        operation_run = repository.reserve(
            replace(
                record.spec,
                owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, owner.ownership.operation_id),
                lifetime=ManagedRunLifetime.OPERATION,
            ),
            output_policy=record.output_policy,
        )
        with patch.object(owner, "borrow", wraps=owner.borrow) as borrow, pytest.raises(ValidationError):
            control(repository, operation_run.identity, **_options(owner, carrier), **_arguments(control))
        borrow.assert_not_called()
        assert carrier.calls == 0
    finally:
        database.close()


@pytest.mark.parametrize(
    "control",
    (
        observe_bound_managed_run,
        read_bound_managed_output,
        collect_bound_managed_result,
        wait_bound_managed_result,
        dispose_bound_managed_run,
    ),
)
def test_resource_and_operation_context_are_incompatible_before_lookup(
    tmp_path: Path, control: Callable[..., object]
) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(_reply)
    try:
        with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
            control(repository, RUN, **_options(owner, carrier, execution_operation=object()), **_arguments(control))
        inspect.assert_not_called()
        assert carrier.calls == 0
    finally:
        database.close()


def test_resource_binding_reconnects_from_new_operation_without_launch(tmp_path: Path) -> None:
    database, repository, owner = _confirmed(tmp_path)
    try:
        original_operation = owner.ownership.operation_id
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
        reconnect = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "reconnect"
        )
        assert reconnect.ownership.operation_id != original_operation
        carrier = ScriptedCarrier(_reply)
        observed = observe_and_reconcile_bound_managed_run(
            repository,
            RUN,
            **_options(reconnect, carrier),  # type: ignore[arg-type]
        )
        assert observed.candidate is not None
        observed = observe_bound_managed_run(repository, RUN, **_options(reconnect, carrier))  # type: ignore[arg-type]
        assert observed.candidate is not None
        output = read_bound_managed_output(
            repository,
            RUN,
            stream=Stream.STDOUT,
            **_options(reconnect, carrier),  # type: ignore[arg-type]
        )
        assert output.output == b"out"
        collected = collect_bound_managed_result(repository, RUN, **_options(reconnect, carrier))  # type: ignore[arg-type]
        assert collected.result.status is not None
        result = wait_bound_managed_result(repository, RUN, **_options(reconnect, carrier))  # type: ignore[arg-type]
        assert result.result.status is not None
        record = repository.inspect(RUN)
        assert record is not None and record.spec.owner == RESOURCE_OWNER
        assert carrier.calls == 9
        stop_carrier = StopCarrier(_response)
        stopped = stop_bound_managed_run(
            repository,
            RUN,
            obligation_id="e" * 32,
            **_options(reconnect, stop_carrier),  # type: ignore[arg-type]
        )
        assert stopped.state is ManagedStopState.ACCEPTED
        disposal_carrier = ExchangeCarrier(_disposed)
        disposed = dispose_bound_managed_run(
            repository,
            RUN,
            obligation_id="f" * 32,
            **_options(reconnect, disposal_carrier),  # type: ignore[arg-type]
        )
        assert disposed.state is DisposalState.DISPOSED
        assert stop_carrier.calls == disposal_carrier.calls == 1
        assert reconnect.list_pending_lifecycle_obligations() == ()
        reconnect.seal_lifecycle_obligations()
        reconnect.record_effects_resolved()
        reconnect.close()
    finally:
        database.close()
