"""SSH environment policy and stream provenance for the shared subprocess pump."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.execution.carrier import Provenance
from agentworks.execution.carriers import _subprocess

if TYPE_CHECKING:
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


def run_process(argv: list[str], *, io: CarrierIO, deadline: Deadline) -> ProcessResult:
    """Run an owned SSH client with isolated pipe settings and SSH provenance."""
    result = _subprocess.run_process(argv, io=io, deadline=deadline, env=_child_environment())
    return replace(
        result,
        stdout=replace(result.stdout, provenance=Provenance.CARRIER_STDOUT),
        stderr=replace(result.stderr, provenance=Provenance.MIXED_STDERR),
    )
