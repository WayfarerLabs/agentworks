"""Independent buffered delivery through an explicitly configured OpenSSH client."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Dispatch,
    ExitStatus,
    Failure,
    Provenance,
    Retention,
)
from agentworks.execution.carriers.ssh._io import run_process
from agentworks.execution.carriers.ssh.connection import build_ssh_argv, validate_connection_files

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline, PreparedInvocation
    from agentworks.execution.carriers.ssh.connection import SSHConnection

_VERSION = re.compile(rb"\AOpenSSH_(?:for_Windows_)?(\d+)\.(\d+)(?:p\d+)?(?:[,\s]|$)")


class SSHCarrier:
    """One command dispatch, without replay or claims about guest cancellation.

    A nonnegative ssh status other than 255 establishes the prepared invocation's
    completion. Status 255 and local signals leave guest completion unknown.
    Raw stderr mixes client diagnostics with the remote stderr channel.
    """

    def __init__(self, connection: SSHConnection) -> None:
        self._connection = connection

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        """Validate locally, then spend the remaining original budget on one attempt."""
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        try:
            validate_connection_files(self._connection)
            argv = build_ssh_argv(self._connection, invocation)
        except (OSError, ValidationError):
            return _not_sent(io, Failure.DISPATCH)
        version = run_process(
            [self._connection.ssh_executable, "-V"], io=CarrierIO(output=Capture(4096)), deadline=deadline
        )
        if version.failure is not None:
            return _not_sent(io, version.failure)
        match = _VERSION.match(version.stderr.data or version.stdout.data)
        if version.exit_status != 0 or match is None or tuple(map(int, match.groups())) < (8, 5):
            return _not_sent(io, Failure.DISPATCH)

        result = run_process(argv, io=io, deadline=deadline)
        completion = (
            ExitStatus(code=result.exit_status)
            if result.exit_status is not None and result.exit_status >= 0 and result.exit_status != 255
            else None
        )
        dispatch = (
            Dispatch.SENT if completion is not None else Dispatch.UNKNOWN if result.started else Dispatch.NOT_SENT
        )
        failure = result.failure
        if result.started and completion is None and failure is None:
            failure = Failure.OBSERVATION
        return CarrierReport(dispatch, completion, result.local_status, result.stdout, result.stderr, failure)


def _not_sent(io: CarrierIO, failure: Failure) -> CarrierReport:
    retention = (
        Retention.SUPPRESSED
        if io.sensitive
        else Retention.CAPTURED
        if isinstance(io.output, Capture)
        else Retention.DISCARDED
    )
    return CarrierReport(
        Dispatch.NOT_SENT,
        stdout=CapturedOutput(provenance=Provenance.CARRIER_STDOUT, retention=retention),
        stderr=CapturedOutput(provenance=Provenance.MIXED_STDERR, retention=retention),
        failure=failure,
    )
