"""Private buffered WSL2 delivery candidate for prepared invocations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Dispatch,
    ExitStatus,
    Failure,
    LiveInput,
    Provenance,
    SinkOutput,
    TerminalInput,
)
from agentworks.execution.carriers._subprocess import run_process

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
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

    A finite nonzero client status observes dispatch but is numerically
    ambiguous between an ordinary exit and a signal. Only zero is candidate
    exact completion evidence.
    Native Windows validation remains required before production adoption.
    """

    def __init__(self, connection: WSL2Connection) -> None:
        self._connection = connection

    @property
    def connection(self) -> WSL2Connection:
        """Literal native route fixed at carrier construction."""
        return self._connection

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        """Reject unsupported input and live output without starting WSL."""
        if isinstance(io.input, TerminalInput):
            raise ValidationError("WSL2 does not support terminal input")
        if isinstance(io.input, LiveInput) or isinstance(io.output, SinkOutput) and io.output.require_live:
            raise ValidationError("WSL2 does not support live standard I/O")

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        """Spend the original deadline on at most one literal WSL exec attempt."""
        self.validate(invocation, io=io)
        if not custody.settled:
            raise StateError("Local delivery custody is unsettled")
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
            custody=custody,
        )
        status = result.exit_status
        # WSL's init normalizes an ordinary exit but otherwise sends raw wait
        # status. Nonzero exits and signals can therefore have the same value.
        observed = result.started and status is not None and 0 <= status <= 255
        completion = ExitStatus(code=0) if observed and status == 0 else None
        dispatch = (
            Dispatch.SENT
            if observed
            else Dispatch.UNKNOWN
            if result.started or result.failure is Failure.OBSERVATION or not custody.settled
            else Dispatch.NOT_SENT
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
