"""SSH I/O selection, child environment policy and stream provenance."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.execution.carrier import Provenance, TerminalInput
from agentworks.execution.carriers import _subprocess

if TYPE_CHECKING:
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import CarrierIO, Deadline
    from agentworks.execution.carriers._subprocess import ProcessResult


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


def run_process(argv: list[str], *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody) -> ProcessResult:
    """Select owned terminal delivery or the byte pump with SSH provenance."""
    if isinstance(io.input, TerminalInput):
        # POSIX dependencies must not affect Windows byte delivery or discovery.
        from agentworks.execution.carriers.ssh._terminal_relay import run_terminal_relay_candidate

        return run_terminal_relay_candidate(argv, io=io, deadline=deadline, custody=custody)
    result = _subprocess.run_process(
        argv,
        io=io,
        deadline=deadline,
        custody=custody,
        env=_child_environment(),
        live_stdio=True,
    )
    return replace(
        result,
        stdout=replace(result.stdout, provenance=Provenance.CARRIER_STDOUT),
        stderr=replace(result.stderr, provenance=Provenance.MIXED_STDERR),
    )
