"""Pure managed request composition before durable reservation."""

from dataclasses import replace

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_request import (
    MAX_ARGV,
    MAX_CONTROL_BYTES,
    MAX_SOURCE_BYTES,
    MAX_STDIN_BYTES,
    RequestError,
    decode_request,
    encode_request,
)
from agentworks.execution._managed_lease_wire import sampled_lease
from agentworks.execution._managed_request_adapter import (
    OPERATION_LEASE_CONTROL_HEADROOM,
    _ManagedBody,
    compose_managed_body,
    compose_managed_request,
)
from agentworks.execution._managed_runs import (
    ManagedOutputMode,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunSpec,
    ManagedShellIdentity,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from agentworks.execution.models import Command, Input, Output, Script, Shell

RUN = ManagedRunIdentity("a" * 32)
pytestmark = pytest.mark.windows


def _spec(shell: ManagedShellIdentity | None = None) -> ManagedRunSpec:
    return ManagedRunSpec(
        ManagedTargetIdentity(ManagedTargetKind.VM, "vm-one", "v1:" + "c" * 64, "00000000-0000-4000-8000-000000000001"),
        IdentityExpectation(1001, 1001, (1001,)),
        shell or ManagedShellIdentity(None, None),
        ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "session-7"),
        ManagedRunLifetime.INDEPENDENT,
    )


def _compose(invocation: Command | Script, **changes: object):
    options = {
        "input": Input.bytes(b"stdin"),
        "output": Output.capture(4096),
        "env": {"LANG": "C.UTF-8"},
        "cwd": "/tmp",
        "sensitive": False,
        "identity": RUN,
        "spec": _spec(),
    }
    options.update(changes)
    return compose_managed_request(invocation, **options)


def test_literal_command_keeps_source_and_stdin_separate() -> None:
    request, policy = _compose(Command(["/usr/bin/printf", "literal ; $()", ""]))
    assert request.argv == ("/usr/bin/printf", "literal ; $()", "")
    assert request.source == b""
    assert request.stdin == b"stdin"
    assert request.environment == (("LANG", "C.UTF-8"),)
    assert policy.mode is ManagedOutputMode.CAPTURE
    assert policy.capture_prefix_bytes == 4096
    assert decode_request(encode_request(request)) == request


@pytest.mark.parametrize(
    ("selection", "resolved"),
    [(Shell.SH, "/bin/sh"), (Shell.BASH, "/bin/bash"), (Shell.USER_DEFAULT, "/usr/bin/bash")],
)
def test_script_uses_exact_supplied_shell(selection: Shell, resolved: str) -> None:
    script = Script("printf 'hello'\n", selection, login=True)
    request, _ = _compose(script, spec=_spec(ManagedShellIdentity(selection, resolved, True)))
    assert request.kind == "script"
    assert request.argv == ()
    assert request.source == b"printf 'hello'\n"
    assert request.stdin == b"stdin"
    assert decode_request(encode_request(request)) == request


def test_sensitive_input_and_explicit_sensitivity_suppress_retention() -> None:
    for input, sensitive in ((Input.sensitive(b"secret"), False), (Input.eof(), True)):
        request, policy = _compose(Command(["/bin/true"]), input=input, sensitive=sensitive)
        assert policy.mode is ManagedOutputMode.SENSITIVITY_SUPPRESSED
        assert policy.capture_prefix_bytes is None
        assert request.output_mode == policy.mode.value
    request, policy = _compose(Command(["/bin/true"]), output=Output.discard())
    assert policy.mode is ManagedOutputMode.DISCARD
    assert request.capture_prefix_bytes is None


def test_unsupported_capture_limit_refuses_even_when_sensitive() -> None:
    with pytest.raises(ValidationError):
        _compose(Command(["/bin/true"]), output=Output.capture(16_777_217), input=Input.sensitive(b"secret"))


def test_multibyte_script_source_exceeds_byte_limit() -> None:
    script = Script("é" * (MAX_SOURCE_BYTES // 2 + 1), Shell.SH)
    with pytest.raises(ValidationError):
        _compose(script, spec=_spec(ManagedShellIdentity(Shell.SH, "/bin/sh")))


@pytest.mark.parametrize(
    ("invocation", "changes"),
    [
        (Command(["/bin/true"]), {"spec": _spec(ManagedShellIdentity(Shell.SH, "/bin/sh"))}),
        (Script("x", Shell.BASH), {"spec": _spec(ManagedShellIdentity(Shell.SH, "/bin/sh"))}),
        (Script("x", Shell.USER_DEFAULT), {"spec": _spec(ManagedShellIdentity(Shell.USER_DEFAULT, "/bin/zsh"))}),
        (
            Script("x", Shell.SH, interactive=True),
            {"spec": _spec(ManagedShellIdentity(Shell.SH, "/bin/sh", interactive=True))},
        ),
        (Command(["/bin/true"]), {"cwd": "relative"}),
        (Command(["/bin/true"]), {"env": {"BASH_ENV": "secret"}}),
        (Command(["/bin/true"]), {"output": Output.capture(16_777_217)}),
        (Command(["/bin/true"] * (MAX_ARGV + 1)), {}),
    ],
)
def test_invalid_or_unsupported_request_refuses_during_preparation(
    invocation: Command | Script, changes: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        _compose(invocation, **changes)


def test_oversize_stdin_refuses_during_preparation() -> None:
    with pytest.raises(ValidationError):
        _compose(Command(["/bin/true"]), input=Input.bytes(b"x" * (MAX_STDIN_BYTES + 1)))


def test_oversize_script_source_refuses_during_preparation() -> None:
    with pytest.raises(ValidationError):
        _compose(Script("x" * (MAX_SOURCE_BYTES + 1), Shell.SH), spec=_spec(ManagedShellIdentity(Shell.SH, "/bin/sh")))


def _operation_spec() -> ManagedRunSpec:
    return replace(
        _spec(),
        lifetime=ManagedRunLifetime.OPERATION,
        owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "e" * 32),
    )


def _body(invocation, *, spec=None, env=None) -> _ManagedBody:
    return compose_managed_body(
        invocation,
        input=Input.bytes(b"stdin"),
        output=Output.capture(4096),
        env=env,
        cwd="/tmp",
        sensitive=False,
        identity=RUN,
        spec=spec or _operation_spec(),
    )


def test_operation_body_freezes_environment_and_complete_encoder_still_requires_lease() -> None:
    environment = {"LANG": "C"}
    body = _body(Command(["/bin/true"]), env=environment)
    environment["LANG"] = "changed"
    environment["NEW"] = "later"
    assert body.request.environment == (("LANG", "C"),)
    assert body.request.operation_lease is None
    with pytest.raises(RequestError):
        encode_request(body.request)
    lease = sampled_lease(body.request.launch, 123)
    completed = replace(body.request, operation_lease=lease)
    assert decode_request(encode_request(completed)) == completed
    request, policy = _compose(Command(["/bin/true"]), spec=_operation_spec(), operation_lease=lease)
    assert request.operation_lease is lease and policy == body.output_policy


def test_operation_body_headroom_accepts_exact_bound_and_refuses_one_byte_more() -> None:
    argv = ("/bin/true",) + ("x" * 4000,) * 7 + ("",)
    independent = _body(Command(argv), spec=_spec())
    size = len(encode_request(independent.request)["request-control"])
    remaining = MAX_CONTROL_BYTES - OPERATION_LEASE_CONTROL_HEADROOM - size
    assert 0 < remaining <= 4096
    exact = _body(Command(argv[:-1] + ("x" * remaining,)))
    lease = sampled_lease(exact.request.launch, 123)
    completed = replace(exact.request, operation_lease=lease)
    assert len(encode_request(completed)["request-control"]) <= MAX_CONTROL_BYTES
    with pytest.raises(ValidationError):
        _body(Command(argv[:-1] + ("x" * (remaining + 1),)))
    # The independent body keeps the full existing control ceiling.
    _body(Command(argv[:-1] + ("x" * (remaining + 1),)), spec=_spec())


@pytest.mark.parametrize(
    "selection,resolved", [(Shell.SH, "/bin/sh"), (Shell.BASH, "/bin/bash"), (Shell.USER_DEFAULT, "/usr/bin/bash")]
)
def test_operation_body_uses_supplied_resolved_shell(selection, resolved) -> None:
    spec = replace(_operation_spec(), shell=ManagedShellIdentity(selection, resolved, True))
    body = _body(Script("printf 'ready'\n", selection, login=True), spec=spec)
    assert body.request.kind == "script" and body.request.source == b"printf 'ready'\n"


def test_operation_complete_wrapper_requires_real_lease() -> None:
    spec = replace(
        _spec(),
        owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32),
        lifetime=ManagedRunLifetime.OPERATION,
    )
    with pytest.raises(ValidationError):
        _compose(Command(["/bin/true"]), spec=spec)
