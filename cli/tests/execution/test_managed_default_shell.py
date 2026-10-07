"""Operation-owned default shell lookup before managed launch admission."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as operation_module
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_access import ManagedJobShellRefusal
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_observe_access import ManagedObserveControlFact
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._workload_shell_protocol import (
    WorkloadShellFailure,
    WorkloadShellResponse,
    decode_workload_shell_request,
    encode_workload_shell_response,
)
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    Retention,
)
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.operations import OperationAttempt, OperationBorrow

from .test_managed_execution_access import bound as bound
from .test_managed_execution_access import view as view
from .test_workload_shell import NONCE, CarrierStub

pytestmark = pytest.mark.windows


@pytest.fixture
def lookup(view, monkeypatch):
    main = view[3]
    shell = CarrierStub(encode_workload_shell_response(NONCE, WorkloadShellResponse(shell="/usr/bin/bash")))
    original = main.execute
    requests, deadlines = [], []

    def execute(invocation, *, io, deadline, custody):
        if io.input.data.startswith(b"{"):
            requests.append(decode_workload_shell_request(io.input.data))
            deadlines.append(deadline)
            return shell.execute(invocation, io=io, deadline=deadline, custody=custody)
        return original(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(main, "validate", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "execute", execute)
    return shell, requests, deadlines


def start(access: ExecutionAccess, **kwargs: Any) -> JobRef:
    return access.start(
        Script("read value; printf '%s' \"$value\"", Shell.USER_DEFAULT, login=True),
        profile=Protection.MANAGED,
        lifetime=Lifetime.OPERATION,
        output=Output.discard(),
        **kwargs,
    )


def no_run(view: Any) -> None:
    database, workflow, _, main, keeper = view
    assert not workflow.views.execution_operation.managed_runs
    assert main.start.calls == keeper.clock.calls == 0
    assert database._conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0


@pytest.mark.parametrize("sudo", [False, True])
@pytest.mark.parametrize("entry", ["start", "run"])
def test_default_shell_uses_selected_identity_and_persists_frozen_body(view, lookup, monkeypatch, sudo, entry):
    database, workflow, access, main, _ = view
    _, requests, deadlines = lookup
    ordinary = IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DEMOTE)
    access._ordinary_plan = ordinary
    access._elevated_plan = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
    environment = {"LANG": "C"}
    saved = []
    response = main.start.response

    def record(request):
        saved.append(request.job)
        return response(request)

    monkeypatch.setattr(main.start, "response", record)
    original = lookup[0].execute

    def mutate(*args, **kwargs):
        environment["LANG"] = "changed"
        return original(*args, **kwargs)

    monkeypatch.setattr(lookup[0], "execute", mutate)
    budget = Deadline.after(5)
    args = dict(sudo=sudo, env=environment, stdin=Input.sensitive(b"literal input"), cwd="/tmp", deadline=budget)
    if entry == "start":
        reference = start(access, **args)
    else:
        result = access.run(
            Script("read value", Shell.USER_DEFAULT, login=True),
            profile=Protection.MANAGED,
            output=Output.discard(),
            **args,
        )
        reference = result.job
    (run,) = workflow.views.execution_operation.managed_runs
    selected = access._elevated_plan if sudo else ordinary
    assert requests[0].identity == selected.expected and deadlines == [budget]
    assert run.receipt.spec.workload == selected.expected
    assert run.receipt.spec.shell.resolved_executable == "/usr/bin/bash"
    assert run.receipt.spec.shell.requested is Shell.USER_DEFAULT and run.receipt.spec.shell.login
    assert reference.run_id == run.receipt.identity.run_id
    assert run._repository.inspect(run.receipt.identity).spec.shell.resolved_executable == "/usr/bin/bash"
    assert saved[0].environment == (("LANG", "C"),) and saved[0].stdin == b"literal input"
    assert saved[0].cwd == "/tmp" and saved[0].output_mode == "sensitivity-suppressed"
    assert saved[0].launch == encode_managed_job_fact(run.receipt)
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("invalid", ["env", "cwd", "input", "output", "interactive"])
def test_pure_invalid_input_never_looks_up_or_reserves(view, lookup, invalid):
    _, workflow, access, _, _ = view
    kwargs: dict[str, Any] = {"env": {"BAD=KEY": "value"}} if invalid == "env" else {}
    if invalid == "cwd":
        kwargs["cwd"] = "relative"
    if invalid == "input":
        kwargs["stdin"] = b"untyped"
    invocation = Script("true", Shell.USER_DEFAULT, interactive=invalid == "interactive")
    with pytest.raises(ValidationError):
        access.start(
            invocation,
            profile=Protection.MANAGED,
            lifetime=Lifetime.OPERATION,
            output=Output.capture(2**30) if invalid == "output" else Output.discard(),
            **kwargs,
        )
    assert lookup[0].calls == 0
    no_run(view)
    workflow.close(cleanup_deadline=Deadline.after(2))


@pytest.mark.parametrize("fault", ["refused", "malformed", "missing", "runtime", "unknown", "failed", "not-sent"])
def test_lookup_failure_never_launches_and_uncertainty_retains_owner(view, lookup, monkeypatch, fault):
    database, workflow, access, _, _ = view
    shell = lookup[0]
    if fault == "refused":
        shell.response = encode_workload_shell_response(
            NONCE, WorkloadShellResponse(failure=WorkloadShellFailure.MISSING)
        )
    if fault in {"malformed", "missing"}:
        shell.response = b"{" if fault == "malformed" else b""
    if fault == "runtime":
        shell.prefix_state = "unusable"
    original = shell.execute

    def execute(*args, **kwargs):
        report = original(*args, **kwargs)
        if fault == "unknown":
            return replace(report, dispatch=Dispatch.UNKNOWN, completion=None)
        if fault == "failed":
            return replace(report, completion=ExitStatus(1))
        if fault == "not-sent":
            return replace(report, dispatch=Dispatch.NOT_SENT, completion=None, failure=Failure.DEADLINE)
        return report

    monkeypatch.setattr(shell, "execute", execute)
    with pytest.raises(ManagedJobShellRefusal):
        start(access)
    no_run(view)
    if fault in {"unknown", "failed"}:
        for _ in range(2):
            with pytest.raises(StateError):
                workflow.close(cleanup_deadline=Deadline.after(2))
        assert database.operations.inspect(workflow.owner.ownership.scope) is not None
        assert workflow.views.execution_operation.unfinished_inline_executions
    else:
        workflow.close(cleanup_deadline=Deadline.after(2))
    assert shell.calls == 1


@pytest.mark.parametrize("point", ["install", "arm", "begin", "settle", "handoff"])
def test_lost_local_reply_retries_exact_bookkeeping_without_lookup_replay(view, lookup, monkeypatch, point):
    _, workflow, access, _, _ = view
    cls, method = (
        (OperationAttempt, "settle")
        if point == "settle"
        else (
            OperationBorrow,
            {
                "install": "install_dispatch_obligation",
                "arm": "arm_dispatch_obligation",
                "begin": "begin_attempt",
                "handoff": "handoff_retained_effect",
            }.get(point, "close"),
        )
    )
    original = getattr(cls, method)
    control = KeyboardInterrupt()

    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise control

    monkeypatch.setattr(cls, method, lost)
    with pytest.raises(KeyboardInterrupt) as caught:
        start(access)
    assert caught.value is control
    assert isinstance(control.__cause__, ManagedObserveControlFact)
    no_run(view)
    calls = lookup[0].calls
    monkeypatch.setattr(cls, method, original)
    workflow.views.execution_operation.retry_inline_bookkeeping()
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert lookup[0].calls == calls


def test_interrupted_lookup_preserves_control_and_original_cause_and_blocks_close(view, lookup, monkeypatch):
    database, workflow, access, _, _ = view
    original_cause = ValueError("private evidence")
    control = KeyboardInterrupt()

    def interrupted(*args, **kwargs):
        raise control from original_cause

    monkeypatch.setattr(lookup[0], "execute", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        start(access)
    assert caught.value is control and isinstance(control.__cause__, ManagedObserveControlFact)
    assert control.__cause__.__cause__ is original_cause
    no_run(view)
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))
    assert database.operations.inspect(workflow.owner.ownership.scope) is not None


def test_fact_allocation_failure_keeps_original_control_and_custody(view, lookup, monkeypatch):
    _, workflow, access, _, _ = view
    cause = ValueError("original cause")
    control = KeyboardInterrupt()

    def interrupted(*args, **kwargs):
        raise control from cause

    def unavailable(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(lookup[0], "execute", interrupted)
    monkeypatch.setattr(operation_module, "ManagedObserveControlFact", unavailable)
    with pytest.raises(KeyboardInterrupt) as caught:
        start(access)
    assert caught.value is control and control.__cause__ is cause
    no_run(view)
    assert workflow.views.execution_operation.unfinished_inline_executions
    with pytest.raises(StateError):
        workflow.close(cleanup_deadline=Deadline.after(2))


@pytest.mark.parametrize("closing", [False, True])
def test_lookup_cannot_start_body_after_budget_expiry_or_close(view, lookup, monkeypatch, closing):
    _, workflow, access, _, _ = view
    operation = workflow.views.execution_operation
    original = operation._observe_workload_shell
    budget = Deadline.after(5)

    def resolved(*args, **kwargs):
        result = original(*args, **kwargs)
        if closing:
            operation.finish()
        else:
            monkeypatch.setattr(Deadline, "expired", property(lambda self: True))
        return result

    monkeypatch.setattr(operation, "_observe_workload_shell", resolved)
    with pytest.raises(StateError if closing else ValidationError):
        start(access, deadline=budget)
    no_run(view)
    monkeypatch.undo()
    workflow.close(cleanup_deadline=Deadline.after(2))


def test_aggregate_close_during_lookup_permanently_blocks_body_admission(view, lookup, monkeypatch):
    _, workflow, access, _, _ = view
    entered, release = threading.Event(), threading.Event()
    controls = []
    original = lookup[0].execute

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    def launch():
        try:
            start(access)
        except BaseException as control:
            controls.append(control)

    monkeypatch.setattr(lookup[0], "execute", blocked)
    caller = threading.Thread(target=launch)
    caller.start()
    try:
        assert entered.wait(2)
        with pytest.raises(StateError):
            workflow.close(cleanup_deadline=Deadline.after(2))
    finally:
        release.set()
        caller.join(3)
    assert not caller.is_alive() and len(controls) == 1 and isinstance(controls[0], StateError)
    no_run(view)
    workflow.close(cleanup_deadline=Deadline.after(2))


def test_successful_helper_reply_without_local_cleanup_retains_exact_delivery(view, lookup, monkeypatch):
    _, workflow, access, _, _ = view
    retained = []
    original = lookup[0].execute

    def unsettled(*args, **kwargs):
        custody = kwargs["custody"]
        retained.append(custody)
        custody.begin_process()
        return original(*args, **kwargs)

    monkeypatch.setattr(lookup[0], "execute", unsettled)
    try:
        with pytest.raises(ManagedJobShellRefusal):
            start(access)
        no_run(view)
        with pytest.raises(StateError):
            workflow.close(cleanup_deadline=Deadline.after(2))
        assert workflow.owner._outstanding_attempt.local_delivery is retained[0]
        assert not retained[0].settled
    finally:
        assert retained[0].close(Deadline.after(2))


@pytest.mark.parametrize("invocation", [Command(["/bin/true"]), Script("true", Shell.SH), Script("true", Shell.BASH)])
def test_literal_and_explicit_shell_skip_lookup(view, lookup, invocation):
    _, workflow, access, _, _ = view
    access.start(invocation, profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard())
    assert lookup[0].calls == 0
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("refuse", [False, True])
def test_selected_route_is_rechecked_before_lookup_and_start(view, lookup, refuse):
    _, workflow, access, _, _ = view
    calls = []

    def require(deadline):
        calls.append(deadline)
        if refuse:
            raise StateError("changed route")

    workflow.views.execution_operation._wsl2_route = SimpleNamespace(require_selected_route=require)
    budget = Deadline.after(5)
    if refuse:
        with pytest.raises(StateError):
            start(access, deadline=budget)
        no_run(view)
        assert lookup[0].calls == 0 and calls == [budget]
    else:
        start(access, deadline=budget)
        assert calls == [budget, budget]
    workflow.views.execution_operation._wsl2_route = None
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux account identity helper")
def test_real_fixed_lookup_under_current_numeric_identity(view, lookup, monkeypatch):
    import pwd

    configured = pwd.getpwuid(os.geteuid()).pw_shell
    _, workflow, access, _, _ = view
    plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )
    runtime = RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
    access._ordinary_plan = plan
    access._runtime_selection = runtime
    operation = workflow.views.execution_operation
    operation._native_binding = replace(operation._native_binding, runtime_selection=runtime)

    def execute(invocation, *, io, deadline, custody):
        completed = subprocess.run(invocation.argv, input=io.input.data, capture_output=True, timeout=5, check=False)
        io.output.stdout.try_write(memoryview(completed.stdout))
        io.output.stderr.try_write(memoryview(completed.stderr))
        delivered = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(
            Dispatch.SENT, ExitStatus(completed.returncode), completed.returncode, delivered, delivered
        )

    monkeypatch.setattr(lookup[0], "execute", execute)
    if configured in {"/bin/sh", "/usr/bin/sh", "/bin/bash", "/usr/bin/bash"}:
        start(access)
        (run,) = operation.managed_runs
        assert run.receipt.spec.workload == plan.expected
        assert run.receipt.spec.shell.resolved_executable == configured
    else:
        with pytest.raises(ManagedJobShellRefusal) as caught:
            start(access)
        result = caught.value.fact.observation
        assert result is not None and result.observation is not None
        assert result.observation.failure is WorkloadShellFailure.UNSUPPORTED
        no_run(view)
    workflow.close(cleanup_deadline=Deadline.after(5))
