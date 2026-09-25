"""Test-only runtime selection and transcript helpers for file exchanges."""

from __future__ import annotations

from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.carrier import PreparedInvocation


def runtime_selection(path: str = "/usr/bin/python3") -> RuntimeSelection:
    """Bind Linux file-helper tests to one explicit interpreter."""
    return RuntimeSelection(RuntimeTargetOS.LINUX, path)


def runtime_nonce(invocation: PreparedInvocation) -> str:
    """Return the selector nonce bound to one prepared invocation."""
    marker = invocation.argv.index("agentworks-runtime-prerequisite")
    return invocation.argv[marker + 1]


def runtime_ready_record(invocation: PreparedInvocation) -> bytes:
    """Return the admitted prefix bound to one prepared invocation."""
    return f"AGW_RUNTIME_1:{runtime_nonce(invocation)}:ready:0\n".encode("ascii")
