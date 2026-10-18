"""Core-supplied resource namespaces fence private independent job controls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agentworks.db import OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._execution_operation import ExecutionOperation
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
from agentworks.execution.carrier import Carrier
from agentworks.operations import OperationOwner

from .test_managed_disposal import ExchangeCarrier, _disposed
from .test_managed_observation import ScriptedCarrier
from .test_managed_observe_access import RESOURCE_OWNER, RUN, _options, _read_operation, _read_options, _reserved
from .test_managed_result import _confirmed, _reply

CONTROLS: tuple[Callable[..., object], ...] = (
    observe_bound_managed_run,
    observe_and_reconcile_bound_managed_run,
    read_bound_managed_output,
    collect_bound_managed_result,
    wait_bound_managed_result,
    dispose_bound_managed_run,
)


def _arguments(control: Callable[..., object]) -> dict[str, object]:
    if control is read_bound_managed_output:
        return {"stream": Stream.STDOUT, "offset": 0, "count": 262144}
    if control is dispose_bound_managed_run:
        return {"obligation_id": "e" * 32}
    return {}


def _control_options(
    control: Callable[..., object], owner: OperationOwner, carrier: Carrier, operation: ExecutionOperation
) -> dict[str, object]:
    if control is dispose_bound_managed_run:
        return _options(owner, carrier)
    return _read_options(owner, carrier, operation)


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
            if control is dispose_bound_managed_run:
                options = _options(owner, carrier, expected_resource_owner=binding)
            else:
                operation = _read_operation(repository, owner, carrier, resource_owner=binding)
                options = _read_options(owner, carrier, operation)
            control(repository, RUN, **options, **_arguments(control))
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
        operation = _read_operation(repository, owner, carrier, resource_owner=foreign)
        options = (
            _options(owner, carrier, expected_resource_owner=foreign)
            if control is dispose_bound_managed_run
            else _read_options(owner, carrier, operation)
        )
        with patch.object(owner, "borrow", wraps=owner.borrow) as borrow, pytest.raises(ValidationError):
            control(repository, RUN, **options, **_arguments(control))
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
    operation = _read_operation(repository, owner, carrier)
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
            control(
                repository,
                operation_run.identity,
                **_control_options(control, owner, carrier, operation),
                **_arguments(control),
            )
        borrow.assert_not_called()
        assert carrier.calls == 0
    finally:
        database.close()


@pytest.mark.parametrize("control", CONTROLS)
def test_read_requires_exact_operation_context_before_lookup(tmp_path: Path, control: Callable[..., object]) -> None:
    database, repository, owner = _reserved(tmp_path)
    carrier = ScriptedCarrier(_reply)
    operation = _read_operation(type(repository)(database), owner, carrier)
    try:
        with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
            options = _control_options(control, owner, carrier, operation)
            if control is dispose_bound_managed_run:
                options["execution_operation"] = operation
            control(repository, RUN, **options, **_arguments(control))
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
        operation = _read_operation(repository, reconnect, carrier)
        options = _read_options(reconnect, carrier, operation)
        observed = observe_and_reconcile_bound_managed_run(
            repository,
            RUN,
            **options,  # type: ignore[arg-type]
        )
        assert observed.candidate is not None
        observed = observe_bound_managed_run(repository, RUN, **options)  # type: ignore[arg-type]
        assert observed.candidate is not None
        output = read_bound_managed_output(
            repository,
            RUN,
            stream=Stream.STDOUT,
            offset=0,
            count=262144,
            **options,  # type: ignore[arg-type]
        )
        assert output.output == b"out"
        collected = collect_bound_managed_result(repository, RUN, **options)  # type: ignore[arg-type]
        assert collected.result.status is not None
        result = wait_bound_managed_result(repository, RUN, **options)  # type: ignore[arg-type]
        assert result.result.status is not None
        record = repository.inspect(RUN)
        assert record is not None and record.spec.owner == RESOURCE_OWNER
        assert carrier.calls == 9
        assert [row.obligation_kind for row in reconnect.list_pending_lifecycle_obligations()] == ["carrier-dispatch"]
        operation.finish()
        disposal_carrier = ExchangeCarrier(_disposed)
        disposed = dispose_bound_managed_run(
            repository,
            RUN,
            obligation_id="f" * 32,
            **_options(reconnect, disposal_carrier),  # type: ignore[arg-type]
        )
        assert disposed.state is DisposalState.DISPOSED
        assert disposal_carrier.calls == 1
        assert reconnect.list_pending_lifecycle_obligations() == ()
        reconnect.seal_lifecycle_obligations()
        reconnect.record_effects_resolved()
        reconnect.close()
    finally:
        database.close()
