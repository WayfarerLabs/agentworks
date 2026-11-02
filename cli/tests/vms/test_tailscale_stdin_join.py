"""The native-transport stdin join that ``vm start``'s rejoin and ``vm rekey`` use."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.ssh import SSHError, SSHResult
from agentworks.vms.initializer.credentials import (
    TAILSCALE_JOIN_STDIN_COMMAND,
    _join_tailscale,
    join_tailscale_ephemerally,
)
from tests.native_exec_support import ExecCall, ExecutionOnlyTransport

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


@pytest.mark.parametrize("auth_key", ["prefix\nsuffix", "prefix\rsuffix", "prefix\0suffix"])
def test_shared_join_rejects_line_unsafe_key_before_transport(auth_key: str) -> None:
    target = _RecordingTransport()

    with pytest.raises(ValidationError) as caught:
        join_tailscale_ephemerally(target, auth_key, timeout=30)  # type: ignore[arg-type]

    assert target.calls == []
    assert auth_key not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_initializer_join_uses_fixed_stdin_once_then_updates_the_ip() -> None:
    db = MagicMock()

    def result_for(call: ExecCall) -> SSHResult:
        stdout = "100.64.0.77\n" if call.command == "tailscale ip -4" else ""
        return SSHResult(returncode=0, stdout=stdout, stderr="")

    target = ExecutionOnlyTransport(result_for)

    result = _join_tailscale(db, "vm1", target, auth_key=_SENTINEL)

    assert result == "100.64.0.77"
    assert target.calls == [
        ExecCall(TAILSCALE_JOIN_STDIN_COMMAND, True, True, None, f"{_SENTINEL}\n"),
        ExecCall("tailscale ip -4", True, True, None, None),
    ]
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
