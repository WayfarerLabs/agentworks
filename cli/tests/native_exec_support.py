"""Shared execution-only transport used to pin native core boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agentworks.ssh import SSHResult
from agentworks.transports import ExecTransport


@dataclass(frozen=True)
class ExecCall:
    """One invocation observed by :class:`ExecutionOnlyTransport`."""

    command: str
    sudo: bool
    check: bool
    timeout: int | None
    input_text: str | None


class ExecutionOnlyTransport(ExecTransport):
    """Minimal native transport with deliberately no rich I/O methods."""

    def __init__(
        self,
        handler: Callable[[ExecCall], SSHResult] | None = None,
        *,
        label: str = "test-native:vm",
    ) -> None:
        self._handler = handler
        self._label = label
        self.calls: list[ExecCall] = []
        self.logger = None
        self.default_timeout = None

    def describe(self) -> str:
        return self._label

    def run(
        self,
        command: str,
        *,
        sudo: bool = False,
        check: bool = True,
        timeout: int | None = None,
        input_text: str | None = None,
    ) -> SSHResult:
        call = ExecCall(command, sudo, check, timeout, input_text)
        self.calls.append(call)
        if self._handler is not None:
            return self._handler(call)
        return SSHResult(returncode=0, stdout="", stderr="")
