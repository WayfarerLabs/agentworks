"""Shared fixed-command Tailscale auth-key delivery."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform.tailscale_join import (
    TAILSCALE_JOIN_STDIN_COMMAND,
    EphemeralTailscaleBootstrap,
    join_tailscale_ephemerally,
)
from agentworks.errors import ProvisioningError
from agentworks.ssh import SSHError, SSHResult
from agentworks.vms.initializer.credentials import _join_tailscale

_SENTINEL = "tskey-join-'swordfish"


class _RecordingTransport:
    logger = None

    def __init__(self, *, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, command: str, **kwargs: object) -> SSHResult:
        self.calls.append((command, kwargs))
        if kwargs.get("input_text") is not None and self.failure is not None:
            raise self.failure
        stdout = "100.64.0.77\n" if command == "tailscale ip -4" else ""
        return SSHResult(returncode=0, stdout=stdout, stderr="")


class _ReadinessTransport:
    logger = None

    def __init__(self, *, fail_command: str, failure: BaseException | None = None) -> None:
        self.fail_command = fail_command
        self.failure = failure if failure is not None else SSHError(f"safe failure for {fail_command}")
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, command: str, **kwargs: object) -> SSHResult:
        self.calls.append((command, kwargs))
        if command == self.fail_command:
            raise self.failure
        return SSHResult(returncode=0, stdout="", stderr="")


def _assert_fixed_stdin_call(call: tuple[str, dict[str, object]], *, timeout: int | None) -> None:
    command, kwargs = call
    assert command == TAILSCALE_JOIN_STDIN_COMMAND
    assert _SENTINEL not in command
    assert kwargs == {"sudo": True, "timeout": timeout, "input_text": f"{_SENTINEL}\n"}


def test_shared_join_preserves_failure_identity_without_secret_diagnostics() -> None:
    failure = SSHError("fixed stdin join failed")
    target = _RecordingTransport(failure=failure)

    with pytest.raises(SSHError) as caught:
        join_tailscale_ephemerally(target, _SENTINEL, timeout=30)  # type: ignore[arg-type]

    assert caught.value is failure
    _assert_fixed_stdin_call(target.calls[0], timeout=30)
    assert _SENTINEL not in str(caught.value)
    assert _SENTINEL not in repr(caught.value)


def test_initializer_join_uses_fixed_stdin_once_then_updates_the_ip() -> None:
    db = MagicMock()
    target = _RecordingTransport()

    result = _join_tailscale(db, "vm1", target, auth_key=_SENTINEL)  # type: ignore[arg-type]

    assert result == "100.64.0.77"
    _assert_fixed_stdin_call(target.calls[0], timeout=None)
    assert target.calls[1] == ("tailscale ip -4", {"sudo": True})
    db.update_vm_tailscale.assert_called_once_with("vm1", "100.64.0.77")


def test_initializer_join_failure_is_same_safe_exception_and_stops_before_ip() -> None:
    failure = SSHError("fixed stdin join failed")
    target = _RecordingTransport(failure=failure)

    with pytest.raises(SSHError) as caught:
        _join_tailscale(MagicMock(), "vm1", target, auth_key=_SENTINEL)  # type: ignore[arg-type]

    assert caught.value is failure
    assert len(target.calls) == 1
    _assert_fixed_stdin_call(target.calls[0], timeout=None)
    assert _SENTINEL not in repr(caught.value)


def test_readiness_exhaustion_raises_typed_without_delivering_key(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _ReadinessTransport(fail_command="echo ok")
    monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda _seconds: None)

    with pytest.raises(ProvisioningError, match="SSH did not become ready") as caught:
        EphemeralTailscaleBootstrap(target).complete(_SENTINEL)  # type: ignore[arg-type]

    assert len(target.calls) == 30
    assert {command for command, _kwargs in target.calls} == {"echo ok"}
    assert all("input_text" not in kwargs for _command, kwargs in target.calls)
    assert _SENTINEL not in repr(caught.value)
    assert _SENTINEL not in repr(target.calls)


def test_cloud_init_wait_failure_raises_typed_without_delivering_key() -> None:
    target = _ReadinessTransport(fail_command="cloud-init status --wait")

    with pytest.raises(ProvisioningError, match="cloud-init did not complete") as caught:
        EphemeralTailscaleBootstrap(target).complete(_SENTINEL)  # type: ignore[arg-type]

    assert target.calls == [
        ("echo ok", {"check": True, "timeout": 10}),
        ("cloud-init status --wait", {"check": True, "timeout": 600}),
    ]
    assert all("input_text" not in kwargs for _command, kwargs in target.calls)
    assert _SENTINEL not in repr(caught.value)
    assert _SENTINEL not in repr(target.calls)


@pytest.mark.parametrize(
    ("failure_command", "expected_commands"),
    [
        ("echo ok", ["echo ok"]),
        ("cloud-init status --wait", ["echo ok", "cloud-init status --wait"]),
    ],
    ids=("ssh-readiness", "cloud-init-wait"),
)
def test_readiness_interrupt_preserves_identity_without_delivering_key(
    failure_command: str,
    expected_commands: list[str],
) -> None:
    interrupt = KeyboardInterrupt(f"interrupted during {failure_command}")
    target = _ReadinessTransport(fail_command=failure_command, failure=interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        EphemeralTailscaleBootstrap(target).complete(_SENTINEL)  # type: ignore[arg-type]

    assert caught.value is interrupt
    assert [command for command, _kwargs in target.calls] == expected_commands
    assert all("input_text" not in kwargs for _command, kwargs in target.calls)
    assert not any("agentworks-bootstrap" in command for command, _kwargs in target.calls)
    assert _SENTINEL not in repr(target.calls)
