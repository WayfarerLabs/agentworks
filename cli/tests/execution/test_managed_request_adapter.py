"""Pure managed request composition before durable reservation."""

from dataclasses import replace

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_job_request import (
    MAX_ARGV,
    MAX_SOURCE_BYTES,
    MAX_STDIN_BYTES,
    decode_request,
    encode_request,
)
from agentworks.execution._managed_request_adapter import compose_managed_request
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


def test_operation_lifetime_refuses() -> None:
    spec = replace(
        _spec(),
        owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "b" * 32),
        lifetime=ManagedRunLifetime.OPERATION,
    )
    with pytest.raises(ValidationError):
        _compose(Command(["/bin/true"]), spec=spec)
