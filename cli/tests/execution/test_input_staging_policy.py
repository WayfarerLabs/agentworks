"""Finite input storage policy at public admission and complete delivery boundaries."""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._capture_bundle import CAPTURE_PROGRAM
from agentworks.execution._execution_result import reduce_owned_inline_result
from agentworks.execution._inline_request import REQUEST_MARKER
from agentworks.execution._managed_request_adapter import compose_managed_body
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import CarrierReport, Deadline, Dispatch, FiniteInput
from agentworks.execution.models import Command, Input, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExitCode
from tests.execution import test_direct_capture as capture_tests
from tests.execution import test_direct_input_operation as input_tests
from tests.execution import test_execution_access as access_tests
from tests.execution import test_managed_request_adapter as managed_tests
from tests.execution._capture_support import PLAN as CAPTURE_PLAN
from tests.execution.test_execution_operation import RecordingCarrier
from tests.execution.test_prepared_helper_stage import RUNTIME

bound = access_tests.bound
owned = input_tests.owned
captured = capture_tests.owned
_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="Linux inline helper execution")


@pytest.mark.parametrize("sensitive", [False, True])
def test_input_storage_choice_is_independent_immutable_and_keyword_only(sensitive):
    payload = b"\0\xffinput"
    value = Input.bytes(payload, sensitive=sensitive, allow_staging=False)
    assert value.data is payload and value.is_sensitive is sensitive and not value.allow_staging
    assert not Input(payload, sensitive, allow_staging=False).allow_staging
    assert not Input.sensitive(payload, allow_staging=False).allow_staging
    assert Input.eof().allow_staging and Input.bytes(payload).allow_staging and Input.sensitive(payload).allow_staging
    with pytest.raises(FrozenInstanceError):
        value.allow_staging = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        Input(payload, sensitive, False)  # type: ignore[call-arg]


@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], object()])
def test_input_storage_choice_requires_exact_bool(invalid):
    for factory in (Input, Input.bytes, Input.sensitive):
        with pytest.raises(ValidationError):
            factory(b"input", allow_staging=invalid)


@pytest.mark.windows
@_LINUX_ONLY
@pytest.mark.parametrize("sensitive", [False, True])
def test_small_input_survives_bound_environment_with_exact_delivery(bound, sensitive, monkeypatch):
    _, owner, operation, carrier, access, _ = bound
    payload = bytes(range(256)) * 3
    stdin = Input.bytes(payload, sensitive=sensitive, allow_staging=False)
    access = access.with_env({"BOUND_INPUT_POLICY": "present"})
    monkeypatch.setattr(
        "agentworks.execution._direct_input_operation.PreparedHelperStage",
        lambda *args, **kwargs: pytest.fail("small input attempted staging"),
    )
    program = (
        "import os,sys; data=sys.stdin.buffer.read(); "
        f"assert data==bytes.fromhex('{payload.hex()}'); "
        "assert os.environ['BOUND_INPUT_POLICY']=='present'; sys.stdout.buffer.write(data)"
    )
    result = access.run(Command((sys.executable, "-c", program)), profile=Protection.DIRECT, stdin=stdin)
    assert result.status == ExitCode(0) and result.owned_cleanup_confirmed
    assert result.stdout.data == (b"" if sensitive else payload)
    assert not stdin.allow_staging and stdin.data is payload and stdin.is_sensitive is sensitive
    assert carrier.calls == 1 and not operation._prepared_stages and owner._active_borrow is None


@pytest.mark.windows
@_LINUX_ONLY
@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("capture_limit", [4096, 8192])
@pytest.mark.parametrize("refusal", ["oversized", "carrier", "live-source"])
def test_refusal_precedes_staging_borrow_and_dispatch_and_releases_admission(
    owned, sensitive, capture_limit, refusal, monkeypatch
):
    owner, operation, carrier = owned
    deadline = Deadline.after(30)
    access = ExecutionAccess(
        operation,
        carrier,
        runtime_selection=RUNTIME,
        ordinary_plan=input_tests.PLAN,
        elevated_plan=None,
        entity_kind="vm",
        entity_name=operation._target.name,
        deadline=lambda: deadline,
    ).with_env({"BOUND_INPUT_POLICY": "present"}, sensitive=sensitive)
    carrier.force_stage = refusal == "carrier"
    if carrier.force_stage:
        validate = carrier.validate

        def refuse_capture(invocation, *, io):
            validate(invocation, io=io)
            if isinstance(io.input, FiniteInput) and io.input.data.startswith(CAPTURE_PROGRAM.prefix + REQUEST_MARKER):
                raise ValidationError("synthetic complete capture carrier refusal")

        monkeypatch.setattr(carrier, "validate", refuse_capture)

    class Source:
        def try_read(self, limit):
            pytest.fail("refused input consumed source")

    source = Source() if refusal == "live-source" else None
    stdin = Input(
        None if source is not None else b"x" * (40_000 if refusal == "oversized" else 10_000),
        sensitive,
        source=source,
        allow_staging=False,
    )
    request = Script("#" + "x" * 40_000, Shell.SH) if source is not None else Command(("/bin/true",))
    before = owner._repository._connection.total_changes
    with monkeypatch.context() as guarded:
        guarded.setattr(owner, "borrow", lambda *args, **kwargs: pytest.fail("refused input borrowed owner"))
        guarded.setattr(
            "agentworks.execution._direct_input_operation.PreparedHelperStage",
            lambda *args, **kwargs: pytest.fail("refused input constructed stage"),
        )
        with pytest.raises(ValidationError):
            access.run(request, profile=Protection.DIRECT, stdin=stdin, output=Output.capture(capture_limit))
    assert owner._repository._connection.total_changes == before
    assert not carrier.requests and carrier.activation_calls == 0 and not carrier.data
    assert owner._active_borrow is None and not owner.list_pending_lifecycle_obligations()
    assert not operation._prepared_stages and not operation._captures
    assert operation._inline_preparation is None and not operation.active_inline_calls
    assert not operation.unfinished_inline_executions and not operation._scratch_cleanup_pending
    carrier.force_stage = False
    later = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    operation.run_inline(
        later,
        Command(("/bin/true",)),
        plan=input_tests.PLAN,
        runtime_selection=RUNTIME,
        deadline=deadline,
        allow_staging=False,
    )
    assert later.calls == 1


@pytest.mark.windows
@pytest.mark.parametrize("method", ["run", "start"])
@pytest.mark.parametrize("lifetime", list(Lifetime))
@pytest.mark.parametrize("sensitive", [False, True])
def test_managed_policy_refuses_before_deadline_or_owner_or_provider(owned, method, lifetime, sensitive, monkeypatch):
    owner, operation, _ = owned
    database = owner._repository._database
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    access = ExecutionAccess(
        operation,
        carrier,
        runtime_selection=RUNTIME,
        ordinary_plan=input_tests.PLAN,
        elevated_plan=None,
        entity_kind="vm",
        entity_name=operation._target.name,
        deadline=lambda: Deadline.after(30),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("managed policy crossed passive admission")

    monkeypatch.setattr(access, "_deadline", forbidden)
    monkeypatch.setattr(owner, "borrow", forbidden)
    monkeypatch.setattr(carrier, "validate", forbidden)
    monkeypatch.setattr(carrier, "execute", forbidden)
    for name in ("start_managed", "start_resource_managed", "observe_job", "wait_job", "_observe_workload_shell"):
        monkeypatch.setattr(operation, name, forbidden)
    before = database._conn.total_changes, owner._repository._connection.total_changes
    with pytest.raises(ValidationError):
        getattr(access, method)(
            Script("true", Shell.USER_DEFAULT),
            profile=Protection.MANAGED,
            lifetime=lifetime,
            stdin=Input.bytes(b"input", sensitive=sensitive, allow_staging=False),
        )
    assert (database._conn.total_changes, owner._repository._connection.total_changes) == before
    assert carrier.calls == 0
    assert owner._active_borrow is None and not owner.list_pending_lifecycle_obligations()


@pytest.mark.parametrize("sensitive", [False, True])
@pytest.mark.parametrize("lifetime", list(Lifetime))
def test_private_managed_body_refuses_before_snapshotting_environment(sensitive, lifetime):
    class Environment(dict):
        def items(self):
            pytest.fail("refused managed policy consumed environment")

    with pytest.raises(ValidationError):
        compose_managed_body(
            Command(("/bin/true",)),
            input=Input.bytes(b"input", sensitive=sensitive, allow_staging=False),
            output=Output.capture(),
            env=Environment(),
            cwd=None,
            sensitive=sensitive,
            identity=managed_tests.RUN,
            spec=managed_tests._operation_spec() if lifetime is Lifetime.OPERATION else managed_tests._spec(),
        )


@pytest.mark.windows
@_LINUX_ONLY
def test_no_input_staging_still_allows_large_output_capture(captured):
    owner, operation, carrier = captured
    source = "/usr/bin/python3 -c 'import sys; assert sys.stdin.buffer.read()==bytes(range(256)); print(42)'"
    outcome = operation.run_inline(
        carrier,
        Script(source, Shell.SH),
        plan=CAPTURE_PLAN,
        runtime_selection=RUNTIME,
        stdin=bytes(range(256)),
        deadline=Deadline.after(30),
        capture_limit=8192,
        allow_staging=False,
    )
    result = reduce_owned_inline_result(outcome)
    assert result.status == ExitCode(0) and result.stdout.data == b"42\n" and result.owned_cleanup_confirmed
    assert not carrier.requests and carrier.activation_calls == 1
    assert not operation._prepared_stages and not operation._captures
    operation.finish()
    assert not owner.list_pending_lifecycle_obligations() and not list(carrier.scratch.iterdir())
