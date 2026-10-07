"""Fixed root-entry packaging and runtime admission for early WSL helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._named_guest_bootstrap import build_named_guest_bootstrap_argv
from agentworks.execution._runtime_prerequisite import (
    MAX_RUNTIME_RECORD_BYTES,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    decode_runtime_prerequisite_record,
)

if TYPE_CHECKING:
    from agentworks.execution._wsl2_lifecycle import OwnedHostClient
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.wsl2 import WSL2Connection


def build_early_argv(connection: WSL2Connection, source: str, nonce: str) -> tuple[str, ...]:
    """Launch fixed core code as root; admission selects the body account."""
    argv, _, _ = build_named_guest_bootstrap_argv(
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT),
        connection.user,
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        fixed_source=source,
        nonce=nonce,
    )
    return (
        connection.wsl_executable,
        "--distribution",
        connection.distribution,
        "--user",
        "root",
        "--exec",
        *argv,
    )


def require_early_runtime(client: OwnedHostClient, nonce: str, deadline: Deadline) -> None:
    """Consume the bounded prerequisite separately from guest evidence."""
    record = client.read_stdout_line(MAX_RUNTIME_RECORD_BYTES + 1, deadline)
    if type(record) is not bytes or len(record) > MAX_RUNTIME_RECORD_BYTES:
        raise ValidationError("WSL2 early helper runtime prerequisite is invalid")
    observation = decode_runtime_prerequisite_record(
        record, nonce=nonce, candidates=("/usr/bin/python3",), system_shim=None
    )
    if observation.state is not RuntimePrerequisiteState.READY or deadline.expired:
        raise ValidationError("WSL2 early helper runtime prerequisite is unavailable")
