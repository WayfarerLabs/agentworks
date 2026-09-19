"""Private buffered WSL2 delivery candidate for prepared invocations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Dispatch,
    ExitStatus,
    Failure,
    Provenance,
)
from agentworks.execution.carriers._subprocess import output_retention, run_process

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline, PreparedInvocation


@dataclass(frozen=True)
class WSL2Connection:
    """Explicit local WSL route; construction performs no discovery or I/O."""

    distribution: str
    user: str
    wsl_executable: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value or "\0" in value for value in (self.distribution, self.user)):
            raise ValidationError("WSL2 requires a nonempty literal distribution and user")
        if (
            not isinstance(self.wsl_executable, str)
            or not self.wsl_executable
            or self.wsl_executable.startswith("-")
            or any(ord(char) < 32 or ord(char) == 127 for char in self.wsl_executable)
        ):
            raise ValidationError("WSL2 executable must be an explicit command name or native path")


class WSL2Carrier:
    """Deliver one prepared invocation through the bound local WSL client.

    The local client's finite status is only candidate completion evidence.
    Native Windows validation remains required before production adoption.
    """

    def __init__(self, connection: WSL2Connection) -> None:
        self._connection = connection

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        """Spend the original deadline on at most one literal WSL exec attempt."""
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        result = run_process(
            [
                self._connection.wsl_executable,
                "--distribution",
                self._connection.distribution,
                "--user",
                self._connection.user,
                "--exec",
                *invocation.argv,
            ],
            io=io,
            deadline=deadline,
        )
        status = result.exit_status
        # The pinned WSL client distinguishes its own failures with -1 and
        # returns the service launch status. Only the guest exit range is
        # candidate completion evidence until Windows-native proof closes.
        completion = ExitStatus(code=status) if result.started and type(status) is int and 0 <= status <= 255 else None
        dispatch = (
            Dispatch.SENT if completion is not None else Dispatch.UNKNOWN if result.started else Dispatch.NOT_SENT
        )
        failure = result.failure
        if result.started and completion is None and failure is None:
            failure = Failure.OBSERVATION
        return CarrierReport(
            dispatch,
            completion,
            result.local_status,
            replace(result.stdout, provenance=Provenance.CARRIER_STDOUT),
            replace(result.stderr, provenance=Provenance.MIXED_STDERR),
            failure,
        )


def _not_sent(io: CarrierIO, failure: Failure) -> CarrierReport:
    retention = output_retention(io)
    return CarrierReport(
        Dispatch.NOT_SENT,
        stdout=CapturedOutput(provenance=Provenance.CARRIER_STDOUT, retention=retention),
        stderr=CapturedOutput(provenance=Provenance.MIXED_STDERR, retention=retention),
        failure=failure,
    )
