"""Foreground managed launch, one relay and durable wait compose one result."""

from __future__ import annotations

import secrets
from dataclasses import replace

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation, ManagedExecutionControlFact
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_job_wire import decode_fact, encode_fact
from agentworks.execution._managed_observation_protocol import ManagedResultControl
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import Deadline, Dispatch, Failure, Retention
from agentworks.execution.models import Command, Input, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, CheckedExecutionError, ExecutionFailure, ExitCode

from . import test_managed_execution_access as managed
from . import test_managed_observation as observed
from .test_managed_lease_exchange import PLAN, RUNTIME
from .test_managed_live_custody import bound as bound
from .test_managed_live_custody import is_live
from .test_managed_live_custody import view as view
from .test_managed_live_exchange import NONCE, Sink, Source

pytestmark = pytest.mark.windows


@pytest.fixture
def access_view(view, monkeypatch):
    _, _, operation, main, _ = view
    deadline = Deadline.after(5)
    access = ExecutionAccess(
        operation,
        main,
        runtime_selection=RUNTIME,
        ordinary_plan=PLAN,
        elevated_plan=PLAN,
        entity_kind="vm",
        entity_name="vm-one",
        deadline=lambda: deadline,
    )
    monkeypatch.setattr(secrets, "token_hex", lambda size: NONCE)
    start = ExecutionOperation.start_managed

    def started(self, *args, **kwargs):
        job = start(self, *args, **kwargs)
        main.launch = encode_managed_job_fact(operation.managed_runs[-1].receipt)
        main.metadata = kwargs["live_io"]
        return job

    monkeypatch.setattr(ExecutionOperation, "start_managed", started)
    original_fact = managed.fact

    def fact(name: FactName, launch: bytes) -> bytes:
        value = decode_fact(original_fact(name, launch))
        if name is FactName.WAIT:
            value["exit_code"] = 0
        return encode_fact(value)

    monkeypatch.setattr(managed, "fact", fact)

    def response(request):
        if request.stream is None:
            return main.observe_response(request)
        end = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
        names = (FactName.LAUNCH, end)
        return observed._records(
            request.nonce,
            ManagedResultControl(names),
            tuple(fact(name, request.expected_launch) for name in names),
        )

    main.observe.response = response
    return operation, access, main, deadline


@pytest.mark.parametrize("live_input,live_output", [(True, True), (True, False), (False, True)])
def test_directions_share_one_start_relay_wait_and_original_deadline(access_view, live_input, live_output):
    operation, access, main, deadline = access_view
    source, stdout, stderr = Source(), Sink(), Sink()
    result = access.run(
        Command(["/bin/true"]),
        profile=Protection.MANAGED,
        stdin=Input.live(source) if live_input else Input.bytes(b"finite"),
        output=Output.live(stdout, stderr) if live_output else Output.discard(),
        check=True,
    )
    assert result.ok and result.status == ExitCode(0) and result.job is not None
    assert main.start.calls == main.relay_calls == 1 and main.observe.calls == 3
    assert main.deadlines == [deadline]
    assert main.metadata.input is live_input and main.metadata.output is live_output
    assert result.stdout.retention is (Retention.DELIVERED if live_output else Retention.DISCARDED)
    assert result.stdout.complete and result.stderr.complete
    assert operation.managed_runs[0].live_relay_attempted
    assert not source.closed and not stdout.closed and not stderr.closed


def test_sensitive_live_output_carries_sensitivity_without_suppressing_explicit_delivery(access_view, monkeypatch):
    _, access, main, _ = access_view
    execute = main.execute
    seen = []

    def sensitive_execute(invocation, *, io, **kwargs):
        if is_live(io):
            seen.append(io.sensitive)
        return execute(invocation, io=io, **kwargs)

    monkeypatch.setattr(main, "execute", sensitive_execute)
    result = access.run(
        Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.live(Sink(), Sink()), sensitive=True
    )
    assert result.ok and result.stdout.retention is Retention.DELIVERED and seen == [True]


def test_sink_failure_is_not_repaired_by_successful_durable_wait(access_view, monkeypatch):
    _, access, main, _ = access_view
    relay = ExecutionOperation.relay_managed

    def failed(self, *args, **kwargs):
        candidate, custody = relay(self, *args, **kwargs)
        return replace(candidate, carrier_failure=Failure.OUTPUT), custody

    monkeypatch.setattr(ExecutionOperation, "relay_managed", failed)
    with pytest.raises(CheckedExecutionError) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.live(Sink(), Sink()), check=True)
    result = caught.value.result
    assert result.status == ExitCode(0) and result.failure is ExecutionFailure.OUTPUT and result.job is not None
    assert main.relay_calls == 1 and main.observe.calls == 3
    assert (caught.value.entity_kind, caught.value.entity_name) == ("vm", "vm-one")


def test_unsettled_relay_does_not_overlap_a_durable_poll(access_view, monkeypatch):
    _, access, main, _ = access_view
    relay = ExecutionOperation.relay_managed

    def uncertain(self, *args, **kwargs):
        candidate, custody = relay(self, *args, **kwargs)
        return candidate, replace(custody, pending_remote_effects=True, requires_owner_retention=True)

    monkeypatch.setattr(ExecutionOperation, "relay_managed", uncertain)
    result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.live(Sink(), Sink()))
    assert result.dispatch is Dispatch.UNKNOWN and result.application_state is ApplicationState.UNKNOWN
    assert result.failure is ExecutionFailure.OBSERVATION and result.job is not None
    assert not result.stdout.complete and not result.owned_cleanup_confirmed and main.observe.calls == 0


def test_acknowledgement_expiry_never_renews_budget_or_starts_relay(access_view, monkeypatch):
    _, access, main, deadline = access_view
    start = ExecutionOperation.start_managed

    def expires(self, *args, **kwargs):
        job = start(self, *args, **kwargs)
        object.__setattr__(deadline, "expires_at", 0.0)
        return job

    monkeypatch.setattr(ExecutionOperation, "start_managed", expires)
    result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.live(Sink(), Sink()))
    assert result.failure is ExecutionFailure.DEADLINE and result.deadline_exceeded and result.job is not None
    assert result.stdout.retention is Retention.DELIVERED and not result.stdout.complete
    assert main.start.calls == 1 and main.relay_calls == main.observe.calls == 0


def test_original_relay_control_escapes_with_safe_job_reference(access_view):
    operation, access, main, _ = access_view
    control = KeyboardInterrupt()
    main.control = control
    with pytest.raises(KeyboardInterrupt) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.live(Sink(), Sink()))
    assert caught.value is control and isinstance(control.__cause__, ManagedExecutionControlFact)
    assert control.__cause__.reference.run_id == operation.managed_runs[0].receipt.identity.run_id
    assert main.relay_calls == 1 and main.observe.calls == 0


@pytest.mark.parametrize("entry", ["start", "independent", "unsupported"])
def test_unsupported_live_shapes_refuse_before_start(access_view, entry):
    operation, access, main, _ = access_view
    options = dict(profile=Protection.MANAGED, output=Output.live(Sink(), Sink()))
    if entry == "unsupported":
        main.live = False
    with pytest.raises((StateError, ValidationError)):
        if entry == "start":
            access.start(Command(["/bin/true"]), lifetime=Lifetime.OPERATION, **options)
        else:
            access.run(
                Command(["/bin/true"]),
                lifetime=Lifetime.INDEPENDENT if entry == "independent" else Lifetime.OPERATION,
                **options,
            )
    assert not operation.managed_runs and main.start.calls == main.relay_calls == 0
