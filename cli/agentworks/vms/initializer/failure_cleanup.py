"""Primary-preserving cleanup helpers for VM initialization failures."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from agentworks import output

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.db import VMRow
    from agentworks.ssh import SSHLogger


class BootstrapSecretCleanup:
    """Mutable owner for secret-bearing Phase A state."""

    def __init__(self) -> None:
        self.git_tokens: dict[str, str] | None = None
        self.logger: SSHLogger | None = None

    def scrub_failure(self) -> None:
        if self.git_tokens is not None:
            self.git_tokens.clear()
        if self.logger is not None:
            self.logger.discard_redactions()


def warn_after_failure(message: str) -> None:
    """Emit a best-effort warning without replacing the active primary."""
    with contextlib.suppress(BaseException):
        output.warn(message)


def close_logger_after_failure(logger: SSHLogger) -> None:
    """Best-effort close that cannot replace an active primary failure."""
    try:
        logger.close()
    except BaseException:
        warn_after_failure("could not close the VM operation log after failure")


def secure_failed_vm_after_failure(
    platform: VMPlatform,
    vm_row: VMRow,
    ctx: RunContext,
    *,
    interrupted: bool,
) -> None:
    """Best-effort provisioning-access cleanup under an active primary."""
    try:
        platform.secure_failed_vm(vm_row, ctx)
    except BaseException:
        state = "interrupted" if interrupted else "failed"
        warn_after_failure(f"could not secure the {state} VM")
