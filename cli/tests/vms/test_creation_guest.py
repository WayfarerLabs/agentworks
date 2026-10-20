"""Prepared creation steps preserve delivery choices and refuse uncertain work."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform.base import ProvisionRequest
from agentworks.debian import DebianRelease
from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.models import Command, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import (
    ApplicationState,
    CheckedExecutionError,
    ExecutionOutput,
    ExecutionResult,
    ExitCode,
)
from agentworks.vms._creation_guest import attest_created_guest, bootstrap_created_guest
from agentworks.vms._native_operation import VMOperation
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY


@pytest.fixture
def creation_request() -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="created",
        debian_release=DebianRelease.BOOKWORM,
        hostname="created",
        system_slug=None,
        instance_marker="a" * 32,
        admin_username="agentworks",
        ssh_public_key=TEST_SSH_PUBLIC_KEY,
        ssh_private_key=None,
        tailscale_auth_key="tskey-test-sensitive",
        progress=MagicMock(),
        cpus=2,
        memory_gib=2,
        disk_gib=20,
        swap_gib=0,
    )


def _completed(data: bytes = b"") -> ExecutionResult:
    return ExecutionResult(
        Dispatch.SENT,
        ApplicationState.COMPLETED,
        ExitCode(0),
        stdout=ExecutionOutput(data, complete=True),
        stderr=ExecutionOutput(complete=True),
        owned_cleanup_confirmed=True,
    )


def test_bootstrap_uses_public_bash_and_nonstaging_secret_input(creation_request: ProvisionRequest) -> None:
    request = creation_request
    operation = MagicMock(spec=VMOperation)
    operation.execution.run.side_effect = [_completed(), _completed(), _completed(b"100.64.0.1\n")]
    deadline = Deadline.after(30)
    assert bootstrap_created_guest(operation, request, deadline) == "100.64.0.1"
    public, join, address = operation.execution.run.call_args_list
    script = public.args[0]
    assert type(script) is Script and script.shell is Shell.BASH
    assert not script.login and not script.interactive
    assert request.tailscale_auth_key not in script.source
    assert public.kwargs["profile"] is Protection.MANAGED
    assert join.args == (Command(("tailscale", "up", "--auth-key=file:/dev/stdin")),)
    assert join.kwargs["profile"] is Protection.DIRECT
    stdin = join.kwargs["stdin"]
    assert stdin.is_sensitive and not stdin.allow_staging
    assert stdin.data == request.tailscale_auth_key.encode()
    assert address.args == (Command(("tailscale", "ip", "-4")),)
    for call in (public, join, address):
        assert call.kwargs["check"] and call.kwargs["sudo"]
        assert call.kwargs["deadline"] is deadline


@pytest.mark.parametrize("step", [0, 1, 2])
def test_bootstrap_does_not_continue_or_replay_after_execution_failure(
    creation_request: ProvisionRequest, step: int
) -> None:
    request = creation_request
    operation = MagicMock(spec=VMOperation)
    failure = CheckedExecutionError(replace(_completed(), status=ExitCode(1)))
    operation.execution.run.side_effect = [*[_completed() for _ in range(step)], failure]
    with pytest.raises(CheckedExecutionError) as caught:
        bootstrap_created_guest(operation, request, Deadline.after(30))
    assert caught.value is failure and operation.execution.run.call_count == step + 1


@pytest.mark.parametrize("data", [b"", b"100.64.0.1\n100.64.0.2\n", b"::1\n", b"\xff"])
def test_bootstrap_refuses_invalid_address(creation_request: ProvisionRequest, data: bytes) -> None:
    request = creation_request
    operation = MagicMock(spec=VMOperation)
    operation.execution.run.side_effect = [_completed(), _completed(), _completed(data)]
    with pytest.raises(StateError):
        bootstrap_created_guest(operation, request, Deadline.after(30))
    assert operation.execution.run.call_count == 3


def test_invalid_join_input_refuses_before_public_configuration(creation_request: ProvisionRequest) -> None:
    request = creation_request
    operation = MagicMock(spec=VMOperation)
    request.tailscale_auth_key = "bad\nkey"
    with pytest.raises(ValidationError):
        bootstrap_created_guest(operation, request, Deadline.after(30))
    operation.execution.run.assert_not_called()


@pytest.mark.parametrize("invalid", [False, True])
def test_release_attestation_uses_prepared_execution_and_pure_parser(invalid: bool) -> None:
    operation = MagicMock(spec=VMOperation)
    data = b"ID=debian\nVERSION_ID=12\nVERSION_CODENAME=bookworm\n" if not invalid else b"\xff"
    operation.execution.run.return_value = _completed(data)
    deadline = Deadline.after(30)
    if invalid:
        with pytest.raises(StateError):
            attest_created_guest(operation, DebianRelease.BOOKWORM, deadline)
    else:
        assert attest_created_guest(operation, DebianRelease.BOOKWORM, deadline) is DebianRelease.BOOKWORM
    call = operation.execution.run.call_args
    assert call.args == (Command(("cat", "/etc/os-release")),)
    assert call.kwargs["profile"] is Protection.DIRECT and call.kwargs["deadline"] is deadline
    assert call.kwargs["check"] and "sudo" not in call.kwargs
