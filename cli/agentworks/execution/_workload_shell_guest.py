"""Fixed Python 3.11 guest entry point for workload shell observation."""

from __future__ import annotations

import os
import stat
import sys

from ._helper_identity import matches_current_identity
from ._workload_shell_protocol import (
    MAX_WORKLOAD_SHELL_REQUEST_BYTES,
    SUPPORTED_SHELLS,
    WorkloadShellFailure,
    WorkloadShellResponse,
    WorkloadShellWireError,
    decode_workload_shell_request,
    encode_workload_shell_response,
)


def _read_request() -> bytes:
    data = bytearray()
    while len(data) <= MAX_WORKLOAD_SHELL_REQUEST_BYTES:
        chunk = os.read(0, MAX_WORKLOAD_SHELL_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _write(data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(1, remaining)
        if written <= 0:
            raise OSError("workload shell response write made no progress")
        remaining = remaining[written:]


def _shell() -> WorkloadShellResponse:
    if sys.platform != "linux":
        return WorkloadShellResponse(failure=WorkloadShellFailure.RUNTIME)
    try:
        import pwd

        configured = pwd.getpwuid(os.geteuid()).pw_shell
    except KeyError:
        return WorkloadShellResponse(failure=WorkloadShellFailure.MISSING)
    except Exception:
        return WorkloadShellResponse(failure=WorkloadShellFailure.LOOKUP)
    if not configured:
        return WorkloadShellResponse(failure=WorkloadShellFailure.MISSING)
    if type(configured) is not str or configured not in SUPPORTED_SHELLS:
        return WorkloadShellResponse(failure=WorkloadShellFailure.UNSUPPORTED)
    try:
        if not stat.S_ISREG(os.stat(configured).st_mode) or not os.access(configured, os.X_OK, effective_ids=True):
            return WorkloadShellResponse(failure=WorkloadShellFailure.UNSUPPORTED)
    except (OSError, ValueError):
        return WorkloadShellResponse(failure=WorkloadShellFailure.MISSING)
    return WorkloadShellResponse(shell=configured)


def main(nonce: str) -> int:
    """Observe the current account only after the final identity transition."""
    try:
        request = decode_workload_shell_request(_read_request())
    except (OSError, WorkloadShellWireError):
        response = WorkloadShellResponse(failure=WorkloadShellFailure.INVALID_REQUEST)
    else:
        if request.nonce != nonce:
            response = WorkloadShellResponse(failure=WorkloadShellFailure.INVALID_REQUEST)
        elif not matches_current_identity(request.identity):
            response = WorkloadShellResponse(failure=WorkloadShellFailure.IDENTITY)
        else:
            response = _shell()
    _write(encode_workload_shell_response(nonce, response))
    return 0
