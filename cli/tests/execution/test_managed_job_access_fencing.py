"""Explicit job control fences current-generation admission and delivery."""

from __future__ import annotations

import time

import pytest

from agentworks.errors import StateError
from agentworks.execution import _managed_operation_keeper as keeper_module
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_exchange import observe_managed_run
from agentworks.execution._managed_observation_protocol import FACT_ORDER, ControllerState, ManagedResultControl
from agentworks.execution.carrier import Deadline, Retention
from agentworks.execution.jobs import JobStream
from agentworks.execution.models import Command, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, ExecutionFailure
from agentworks.operations import OperationOwner

from . import test_managed_observation as observation_tests
from .test_managed_execution_access import bound as bound
from .test_managed_execution_access import fact
from .test_managed_execution_access import view as view


@pytest.mark.parametrize("takeover", ["admission", "stop", "observation"])
def test_stop_takeover_refuses_old_generation_delivery(view, monkeypatch, takeover):
    database, workflow, access, _, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    recovered = [False]

    def recover() -> None:
        if not recovered[0]:
            OperationOwner.recover(database.operations, workflow.owner.ownership, "f" * 32)
            recovered[0] = True

    if takeover == "admission":
        recover()
    elif takeover == "stop":
        carrier = keeper.stop
        original = carrier.validate

        def validate(*args, **kwargs):
            original(*args, **kwargs)
            recover()

        monkeypatch.setattr(carrier, "validate", validate)
    else:
        original_observe = observe_managed_run

        def observe(*args, **kwargs):
            recover()
            return original_observe(*args, **kwargs)

        monkeypatch.setattr(keeper_module, "observe_managed_run", observe)
    if takeover == "observation":
        result = access.stop(reference)
        assert result.accepted is True and not result.terminated
        assert result.failure is ExecutionFailure.OBSERVATION
        assert keeper.stop.calls == 1 and keeper.observe.calls == 0
    else:
        with pytest.raises(StateError):
            access.stop(reference)
        assert keeper.stop.calls == keeper.observe.calls == 0
        assert run.keeper._stop.is_set() is (takeover != "admission")
    assert not run.cleanup_complete


@pytest.mark.parametrize("sensitive", [False, True])
def test_policy_closed_empty_output_is_eof_without_capture(view, sensitive):
    _, workflow, access, main, _ = view
    reference = access.start(
        Command(["/bin/true"]),
        profile=Protection.MANAGED,
        lifetime=Lifetime.OPERATION,
        output=Output.discard(),
        sensitive=sensitive,
    )

    def response(request):
        end = decode_fact(fact(FactName.STDOUT_END, request.expected_launch))
        if sensitive:
            end["disposition"] = "sensitivity-suppressed"
        return observation_tests._records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, FactName.STDOUT_END)),
            (request.expected_launch, encode_fact(end)),
        )

    main.observe.response = response
    result = access.read_output(reference, stream=JobStream.STDOUT)
    assert result.eof and not result.capture_complete and result.data == b""
    assert result.retention is (Retention.SUPPRESSED if sensitive else Retention.DISCARDED)
    assert result.failure is None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("wait_fact", ["present", "absent", "late"])
def test_operation_wait_polls_until_positive_controller_closure(view, monkeypatch, wait_fact):
    _, workflow, access, main, keeper = view
    reference = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    snapshots = [0]

    def response(request):
        if request.stream is not None:
            name = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
            return observation_tests._records(
                request.nonce,
                ManagedResultControl((FactName.LAUNCH, name)),
                (request.expected_launch, fact(name, request.expected_launch)),
            )
        snapshots[0] += 1
        main.controller = ControllerState.RUNNING if snapshots[0] == 1 else ControllerState.EXITED
        main.facts = (
            FACT_ORDER
            if wait_fact == "present" or (wait_fact == "late" and snapshots[0] > 1)
            else (tuple(name for name in FACT_ORDER if name is not FactName.WAIT))
        )
        return main.observe_response(request)

    main.observe.response = response
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    result = access.wait(reference)
    assert snapshots[0] == 2 and result.owned_cleanup_confirmed
    assert result.application_state is (
        ApplicationState.UNKNOWN if wait_fact == "absent" else ApplicationState.COMPLETED
    )
    assert (result.status is None) is (wait_fact == "absent")
    assert keeper.stop.calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))
