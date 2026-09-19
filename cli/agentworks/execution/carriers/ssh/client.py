"""Independent buffered delivery through an explicitly configured OpenSSH client."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Discard,
    Dispatch,
    EndOfInput,
    ExitStatus,
    Failure,
    FiniteInput,
    Provenance,
)
from agentworks.execution.carriers._subprocess import output_retention, run_process
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
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

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        """Refuse unsupported shared I/O shapes without connection or process work."""
        if not isinstance(io.input, EndOfInput | FiniteInput):
            raise ValidationError("Buffered SSH requires EOF or finite input")
        if not isinstance(io.output, Capture | Discard):
            raise ValidationError("Buffered SSH requires captured or discarded output")

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        """Validate locally, then spend the remaining original budget on one attempt."""
        self.validate(invocation, io=io)
        if not custody.settled:
            raise StateError("Local delivery custody is unsettled")
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        try:
            trust = admit_connection(self._connection)
            argv = build_ssh_argv(self._connection, invocation, trust=trust)
        except (OSError, StateError, ValidationError):
            return _not_sent(io, Failure.DISPATCH)
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        version_failure = check_client_version(self._connection, deadline=deadline, custody=custody)
        if version_failure is not None:
            return _not_sent(io, version_failure)

        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        result = run_process(argv, io=io, deadline=deadline, custody=custody, env=_child_environment())
        completion = (
            ExitStatus(code=result.exit_status)
            if result.exit_status is not None and 0 <= result.exit_status < 255
            else None
        )
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


def _child_environment() -> dict[str, str] | None:
    if os.name != "nt":
        return None
    # These describe OpenSSH's parent's handles, never our new Python pipes.
    # https://github.com/PowerShell/openssh-portable/blob/v9.5.0.0/contrib/win32/win32compat/w32fd.c#L115-L128
    # Newer clients also accept OPENSSH_STDIO_MODE to select handle semantics.
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in ("C28FC6F98A2C44ABBBD89D6A3037D0D9_POSIX_FD_STATE", "OPENSSH_STDIO_MODE")
    }


def check_client_version(connection: SSHConnection, *, deadline: Deadline, custody: LocalDeliveryCustody) -> Failure | None:
    """Check the selected installed client within the original operation budget."""
    version = run_process([connection.ssh_executable, "-V"], io=CarrierIO(output=Capture(4096)), deadline=deadline, custody=custody, env=_child_environment())
    if version.failure is not None:
        return version.failure
    if not custody.settled:
        return Failure.OBSERVATION
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
