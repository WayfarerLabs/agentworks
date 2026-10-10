"""Independent byte delivery through an explicitly configured OpenSSH client."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import (
    Capture,
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Dispatch,
    EndOfInput,
    ExitStatus,
    Failure,
    FiniteInput,
    LiveInput,
    Provenance,
)
from agentworks.execution.carriers._subprocess import output_retention
from agentworks.execution.carriers.ssh._io import run_process
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
        return ChannelFeatures(live_stdio=True)

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        """Refuse input this adapter cannot deliver before effectful SSH admission."""
        if not isinstance(io.input, EndOfInput | FiniteInput | LiveInput):
            raise ValidationError("SSH byte delivery requires EOF, finite or live byte input")

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
        except (OSError, StateError, ValidationError):
            return _not_sent(io, Failure.DISPATCH)
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        try:
            executable = resolve_client_executable(self._connection)
        except (OSError, ValidationError):
            return _not_sent(io, Failure.DISPATCH)
        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        version_failure = check_client_version(executable, deadline=deadline, custody=custody)
        if version_failure is not None:
            return _not_sent(io, version_failure)

        if deadline.expired:
            return _not_sent(io, Failure.DEADLINE)
        argv = build_ssh_argv(self._connection, invocation, trust=trust, executable=executable)
        result = run_process(argv, io=io, deadline=deadline, custody=custody)
        completion = (
            ExitStatus(code=result.exit_status)
            if result.exit_status is not None and 0 <= result.exit_status < 255
            else None
        )
        dispatch = (
            Dispatch.SENT
            if completion is not None
            else Dispatch.UNKNOWN
            if result.started or result.failure is Failure.OBSERVATION
            else Dispatch.NOT_SENT
        )
        failure = result.failure
        if result.started and completion is None and failure is None:
            failure = Failure.OBSERVATION
        return CarrierReport(
            dispatch,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            failure,
        )


def resolve_client_executable(connection: SSHConnection) -> str:
    """Pin this operation's installed client without implicit cwd search.

    Each PATH candidate has an absolute directory before shutil.which examines
    native executable suffixes. Bare-name which and Windows process creation
    can otherwise prepend cwd even when PATH does not name it. Explicit PATH
    entries, including relative directories, remain operator selections. Empty
    Windows components do not select cwd; POSIX retains its PATH semantics.
    """
    executable = connection.ssh_executable
    if os.path.isabs(executable):
        # PATHEXT must not redirect an explicit path to another client.
        if os.path.isfile(executable) and os.access(executable, os.F_OK | os.X_OK):
            return executable
        raise ValidationError("SSH requires the selected installed executable")
    search_path = os.environ.get("PATH", "")
    candidates = (
        tuple(
            str((Path(directory) / executable).absolute())
            for directory in search_path.split(os.pathsep)
            if directory or os.name != "nt"
        )
        if search_path
        else ()
    )
    for candidate in candidates:
        selected = shutil.which(candidate)
        if selected is not None:
            return selected
    raise ValidationError("SSH requires the selected installed executable")


def check_client_version(executable: str, *, deadline: Deadline, custody: LocalDeliveryCustody) -> Failure | None:
    """Check the selected installed client within the original operation budget."""
    version = run_process([executable, "-V"], io=CarrierIO(output=Capture(4096)), deadline=deadline, custody=custody)
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
