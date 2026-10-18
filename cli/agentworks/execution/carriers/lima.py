"""Private buffered host-local Lima delivery for prepared invocations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from agentworks.naming import NAME_RE

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Deadline, PreparedInvocation


@dataclass(frozen=True)
class LimaConnection:
    """Explicit local route from an adapter author; no discovery or activation."""

    instance: str = field(repr=False)
    limactl_executable: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.instance, str) or NAME_RE.fullmatch(self.instance) is None:
            raise ValidationError("Lima requires a literal Agentworks instance name")
        if (
            not isinstance(self.limactl_executable, str)
            or not self.limactl_executable
            or self.limactl_executable.startswith("-")
            or any(ord(char) < 32 or ord(char) == 127 for char in self.limactl_executable)
        ):
            raise ValidationError("Lima executable must be an explicit command name or native path")


class LimaCarrier:
    """Deliver once through the bound host-local Lima client.

    Only an observed zero client exit is candidate completion of the outer
    guest command chain. Other statuses cannot distinguish a local Lima
    refusal from a remote exit. Neither proves bootstrap or application entry.
    Lima's outer login shell can run account startup hooks before the trampoline.
    Native Lima validation remains required before production adoption.
    """

    def __init__(self, connection: LimaConnection) -> None:
        self._connection = connection
        self._channel_features = ChannelFeatures()

    @property
    def connection(self) -> LimaConnection:
        """Literal local route fixed at carrier construction."""
        return self._connection

    @property
    def features(self) -> ChannelFeatures:
        return self._channel_features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        """Reject unsupported input and live output without starting Lima."""
        if isinstance(io.input, TerminalInput):
            raise ValidationError("Lima does not support terminal input")
        if isinstance(io.input, LiveInput) or isinstance(io.output, SinkOutput) and io.output.require_live:
            raise ValidationError("Lima does not support live standard I/O")

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        """Spend the original deadline on at most one local Lima shell attempt."""
        self.validate(invocation, io=io)
        if not custody.settled:
            raise StateError("Local delivery custody is unsettled")
        result = run_process(
            [
                self._connection.limactl_executable,
                "shell",
                "--tty=false",
                "--start=false",
                "--reconnect=false",
                "--preserve-env=false",
                "--workdir=/",
                # Lima invokes this bootstrap shell with -l -c, including login
                # startup hooks, independently of the application's shell policy.
                "--shell=/bin/bash",
                self._connection.instance,
                "--",
                # A fixed command bypasses Lima's leading env-assignment
                # heuristic. Quoted positional expansion into exec preserves
                # even assignment-looking or option-looking executable names.
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                'exec -- "$@"',
                "agentworks-lima-bootstrap",
                *invocation.argv,
            ],
            io=io,
            deadline=deadline,
            custody=custody,
        )
        completion = ExitStatus(code=0) if result.started and result.exit_status == 0 else None
        dispatch = (
            Dispatch.SENT
            if completion is not None
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
