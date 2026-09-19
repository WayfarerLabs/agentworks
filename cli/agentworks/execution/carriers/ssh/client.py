"""Independent buffered delivery through an explicitly configured OpenSSH client."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
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
)
from agentworks.execution.carriers.ssh._io import output_retention, run_process
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline, PreparedInvocation
    from agentworks.execution.carriers.ssh.connection import SSHConnection

_VERSION = re.compile(rb"\AOpenSSH_(?:for_Windows_)?(\d+)\.(\d+)(?:p\d+)?(?:[,\s]|$)")


class SSHCarrier:
    """One command dispatch, without replay or claims about guest cancellation.

    A status from 0 through 254 observes remote command evaluation, including
    account-shell startup. It alone proves neither bootstrap nor application
    execution. Status 255, signals and Windows crash codes leave it unknown.
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
            trust = admit_connection(self._connection)
            argv = build_ssh_argv(self._connection, invocation, trust=trust)
        except (OSError, StateError, ValidationError):
            return _not_sent(io, Failure.DISPATCH)
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        version_failure = check_client_version(self._connection, deadline=deadline)
        if version_failure is not None:
            return _not_sent(io, version_failure)

        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        result = run_process(argv, io=io, deadline=deadline)
        completion = (
            ExitStatus(code=result.exit_status)
            if result.exit_status is not None and 0 <= result.exit_status < 255
            else None
        )
        dispatch = (
            Dispatch.SENT if completion is not None else Dispatch.UNKNOWN if result.started else Dispatch.NOT_SENT
        )
        failure = result.failure
        if result.started and completion is None and failure is None:
            failure = Failure.OBSERVATION
        return CarrierReport(dispatch, completion, result.local_status, result.stdout, result.stderr, failure)


def check_client_version(connection: SSHConnection, *, deadline: Deadline) -> Failure | None:
    """Check the selected installed client within the original operation budget."""
    version = run_process([connection.ssh_executable, "-V"], io=CarrierIO(output=Capture(4096)), deadline=deadline)
    if version.failure is not None:
        return version.failure
    match = _VERSION.match(version.stderr.data)
    if version.exit_status != 0 or match is None or tuple(map(int, match.groups())) < (8, 5):
        return Failure.DISPATCH
    return None


def _not_sent(io: CarrierIO, failure: Failure) -> CarrierReport:
    retention = output_retention(io)
    return CarrierReport(
        Dispatch.NOT_SENT,
        stdout=CapturedOutput(provenance=Provenance.CARRIER_STDOUT, retention=retention),
        stderr=CapturedOutput(provenance=Provenance.MIXED_STDERR, retention=retention),
        failure=failure,
    )
